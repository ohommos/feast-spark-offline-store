from typing import List, Union, Optional, Dict
from pydantic import StrictStr
from datetime import datetime

import pandas
import pyspark
import pyarrow
import numpy as np
import pandas as pd
from pytz import utc

from feast.registry import Registry
from feast import FeatureView, OnDemandFeatureView
from feast.data_source import DataSource
from feast.repo_config import FeastConfigBaseModel, RepoConfig
from feast.infra.offline_stores.offline_store import OfflineStore, RetrievalJob
from feast.infra.offline_stores import offline_utils
from feast.errors import InvalidEntityType

from pyspark.sql import SparkSession
from pyspark import SparkConf
from feast_spark_offline_store.spark_source import SparkSource
from feast_spark_offline_store.spark_type_map import spark_schema_to_np_dtypes


class SparkOfflineStoreConfig(FeastConfigBaseModel):
    type: StrictStr = "spark"
    """ Offline store type selector"""

    spark_conf: Optional[Dict[str, str]] = None
    """ Configuration overlay for the spark session """
    # to ensure sparksession is the correct config, if not created yet
    # sparksession is not serializable and we dont want to pass it around as an argument


class SparkOfflineStore(OfflineStore):
    @staticmethod
    def pull_latest_from_table_or_query(
        config: RepoConfig,
        data_source: DataSource,
        join_key_columns: List[str],
        feature_name_columns: List[str],
        event_timestamp_column: str,
        created_timestamp_column: Optional[str],
        start_date: datetime,
        end_date: datetime,
    ) -> RetrievalJob:
        spark_session = get_spark_session_or_start_new_with_repoconfig(
            config.offline_store
        )

        assert isinstance(config.offline_store, SparkOfflineStoreConfig)
        assert isinstance(data_source, SparkSource)

        print("Pulling latest features from spark offline store")

        from_expression = data_source.get_table_query_string()

        partition_by_join_key_string = ", ".join(join_key_columns)
        if partition_by_join_key_string != "":
            partition_by_join_key_string = (
                "PARTITION BY " + partition_by_join_key_string
            )
        timestamps = [event_timestamp_column]
        if created_timestamp_column:
            timestamps.append(created_timestamp_column)
        timestamp_desc_string = " DESC, ".join(timestamps) + " DESC"
        field_string = ", ".join(join_key_columns + feature_name_columns + timestamps)

        start_date = _format_datetime(start_date)
        end_date = _format_datetime(end_date)

        query = f"""
                SELECT {field_string}
                FROM (
                    SELECT {field_string},
                    ROW_NUMBER() OVER({partition_by_join_key_string} ORDER BY {timestamp_desc_string}) AS feast_row_
                    FROM {from_expression} t1
                    WHERE {event_timestamp_column} BETWEEN TIMESTAMP('{start_date}') AND TIMESTAMP('{end_date}')
                ) t2
                WHERE feast_row_ = 1
                """

        return SparkRetrievalJob(
            spark_session=spark_session,
            query=query,
            full_feature_names=False,
            on_demand_feature_views=None,
        )

    @staticmethod
    def get_historical_features(
        config: RepoConfig,
        feature_views: List[FeatureView],
        feature_refs: List[str],
        entity_df: Union[pandas.DataFrame, str],
        registry: Registry,
        project: str,
        full_feature_names: bool = False,
    ) -> RetrievalJob:
        assert isinstance(config.offline_store, SparkOfflineStoreConfig)

        spark_session = get_spark_session_or_start_new_with_repoconfig(
            config.offline_store
        )

        table_name = offline_utils.get_temp_entity_table_name()

        entity_schema = _upload_entity_df_and_get_entity_schema(
            spark_session, table_name, entity_df
        )

        entity_df_event_timestamp_col = (
            offline_utils.infer_event_timestamp_from_entity_df(entity_schema)
        )

        expected_join_keys = offline_utils.get_expected_join_keys(
            project, feature_views, registry
        )

        offline_utils.assert_expected_columns_in_entity_df(
            entity_schema, expected_join_keys, entity_df_event_timestamp_col
        )

        query_context = offline_utils.get_feature_view_query_context(
            feature_refs,
            feature_views,
            registry,
            project,
        )

        query = offline_utils.build_point_in_time_query(
            query_context,
            left_table_query_string=table_name,
            entity_df_event_timestamp_col=entity_df_event_timestamp_col,
            entity_df_columns=entity_schema.keys(),
            query_template=MULTIPLE_FEATURE_VIEW_POINT_IN_TIME_JOIN,
            full_feature_names=full_feature_names,
        )

        return SparkRetrievalJob(
            spark_session=spark_session,
            query=query,
            full_feature_names=full_feature_names,
            on_demand_feature_views=OnDemandFeatureView.get_requested_odfvs(
                feature_refs, project, registry
            ),
        )


# TODO fix internal abstract methods _to_df_internal _to_arrow_internal
class SparkRetrievalJob(RetrievalJob):
    def __init__(
        self,
        spark_session: SparkSession,
        query: str,
        full_feature_names: bool,
        on_demand_feature_views: Optional[List[OnDemandFeatureView]],
    ):
        super().__init__()
        self.spark_session = spark_session
        self.query = query
        self._full_feature_names = full_feature_names
        self._on_demand_feature_views = on_demand_feature_views

    @property
    def full_feature_names(self) -> bool:
        return self._full_feature_names

    @property
    def on_demand_feature_views(self) -> Optional[List[OnDemandFeatureView]]:
        return self._on_demand_feature_views

    def to_spark_df(self) -> pyspark.sql.DataFrame:
        statements = self.query.split(
            "---EOS---"
        )  # TODO can do better than this dirty split
        *_, last = map(self.spark_session.sql, statements)
        return last

    def to_df(self) -> pandas.DataFrame:
        return self.to_spark_df().toPandas()  # noqa, DataFrameLike instead of DataFrame

    def _to_df_internal(self) -> pd.DataFrame:
        """Return dataset as Pandas DataFrame synchronously"""
        pass

    def _to_arrow_internal(self) -> pyarrow.Table:
        """Return dataset as pyarrow Table synchronously"""
        pass

    def to_arrow(self) -> pyarrow.Table:
        df = self.to_df()
        return pyarrow.Table.from_pandas(df)  # noqa


def _upload_entity_df_and_get_entity_schema(
    spark_session, table_name, entity_df
) -> Dict[str, np.dtype]:
    if isinstance(entity_df, pd.DataFrame):
        spark_session.createDataFrame(entity_df).createOrReplaceTempView(table_name)
        return dict(zip(entity_df.columns, entity_df.dtypes))
    elif isinstance(entity_df, str):
        spark_session.sql(entity_df).createOrReplaceTempView(table_name)
        limited_entity_df = spark_session.table(table_name)
        # limited_entity_df = spark_session.table(table_name).limit(1).toPandas()

        return dict(
            zip(
                limited_entity_df.columns,
                spark_schema_to_np_dtypes(limited_entity_df.dtypes),
            )
        )
    else:
        raise InvalidEntityType(type(entity_df))


def get_spark_session_or_start_new_with_repoconfig(
    store_config: SparkOfflineStoreConfig,
) -> SparkSession:
    spark_session = SparkSession.getActiveSession()

    if not spark_session:
        spark_builder = SparkSession.builder
        spark_conf = store_config.spark_conf

        if spark_conf:
            spark_builder = spark_builder.config(
                conf=SparkConf().setAll(spark_conf.items())
            )  # noqa

        spark_session = spark_builder.getOrCreate()

    spark_session.conf.set(
        "spark.sql.parser.quotedRegexColumnNames", "true"
    )  # important!

    return spark_session


def _format_datetime(t: datetime):
    # Since Hive does not support timezone, need to transform to utc.
    if t.tzinfo:
        t = t.astimezone(tz=utc)
    t = t.strftime("%Y-%m-%d %H:%M:%S.%f")
    return t


MULTIPLE_FEATURE_VIEW_POINT_IN_TIME_JOIN = """/*
 Compute a deterministic hash for the `left_table_query_string` that will be used throughout
 all the logic as the field to GROUP BY the data
*/
CREATE OR REPLACE TEMPORARY VIEW entity_dataframe AS (
    SELECT *,
        {{entity_df_event_timestamp_col}} AS entity_timestamp
        {% for featureview in featureviews %}
            ,CONCAT(
                {% for entity in featureview.entities %}
                    CAST({{entity}} AS STRING),
                {% endfor %}
                CAST({{entity_df_event_timestamp_col}} AS STRING)
            ) AS {{featureview.name}}__entity_row_unique_id
        {% endfor %}
    FROM {{ left_table_query_string }}
);


---EOS---


-- Start create temporary table *__base
{% for featureview in featureviews %}

CREATE OR REPLACE TEMPORARY VIEW {{ featureview.name }}__base AS
WITH {{ featureview.name }}__entity_dataframe AS (
    SELECT
        {{ featureview.entities | join(', ')}},
        entity_timestamp,
        {{featureview.name}}__entity_row_unique_id
    FROM entity_dataframe
    GROUP BY {{ featureview.entities | join(', ')}}, entity_timestamp, {{featureview.name}}__entity_row_unique_id
),

/*
 This query template performs the point-in-time correctness join for a single feature set table
 to the provided entity table.

 1. We first join the current feature_view to the entity dataframe that has been passed.
 This JOIN has the following logic:
    - For each row of the entity dataframe, only keep the rows where the `event_timestamp_column`
    is less than the one provided in the entity dataframe
    - If there a TTL for the current feature_view, also keep the rows where the `event_timestamp_column`
    is higher the the one provided minus the TTL
    - For each row, Join on the entity key and retrieve the `entity_row_unique_id` that has been
    computed previously

 The output of this CTE will contain all the necessary information and already filtered out most
 of the data that is not relevant.
*/

{{ featureview.name }}__subquery AS (
    SELECT
        {{ featureview.event_timestamp_column }} as event_timestamp,
        {{ featureview.created_timestamp_column ~ ' as created_timestamp,' if featureview.created_timestamp_column else '' }}
        {{ featureview.entity_selections | join(', ')}},
        {% for feature in featureview.features %}
            {{ feature }} as {% if full_feature_names %}{{ featureview.name }}__{{feature}}{% else %}{{ feature }}{% endif %}{% if loop.last %}{% else %}, {% endif %}
        {% endfor %}
    FROM {{ featureview.table_subquery }} AS subquery
    INNER JOIN (
        SELECT MAX(entity_timestamp) as max_entity_timestamp_
               {% if featureview.ttl == 0 %}{% else %}
               ,(MIN(entity_timestamp) - interval '{{ featureview.ttl }}' second) as min_entity_timestamp_
               {% endif %}
        FROM entity_dataframe
    ) AS temp
    ON (
        {{ featureview.event_timestamp_column }} <= max_entity_timestamp_
        {% if featureview.ttl == 0 %}{% else %}
        AND {{ featureview.event_timestamp_column }} >=  min_entity_timestamp_
        {% endif %}
    )
)
SELECT
    subquery.*,
    entity_dataframe.entity_timestamp,
    entity_dataframe.{{featureview.name}}__entity_row_unique_id
FROM {{ featureview.name }}__subquery AS subquery
INNER JOIN (
    SELECT *
    {% if featureview.ttl == 0 %}{% else %}
    , (entity_timestamp - interval '{{ featureview.ttl }}' second) as ttl_entity_timestamp
    {% endif %}
    FROM {{ featureview.name }}__entity_dataframe
) AS entity_dataframe
ON (
    subquery.event_timestamp <= entity_dataframe.entity_timestamp

    {% if featureview.ttl == 0 %}{% else %}
    AND subquery.event_timestamp >= entity_dataframe.ttl_entity_timestamp
    {% endif %}

    {% for entity in featureview.entities %}
    AND subquery.{{ entity }} = entity_dataframe.{{ entity }}
    {% endfor %}
);

{% endfor %}
-- End create temporary table *__base

---EOS---

{% for featureview in featureviews %}

{% if loop.first %}WITH{% endif %}

/*
 2. If the `created_timestamp_column` has been set, we need to
 deduplicate the data first. This is done by calculating the
 `MAX(created_at_timestamp)` for each event_timestamp.
 We then join the data on the next CTE
*/
{% if featureview.created_timestamp_column %}
{{ featureview.name }}__dedup AS (
    SELECT
        {{featureview.name}}__entity_row_unique_id,
        event_timestamp,
        MAX(created_timestamp) as created_timestamp
    FROM {{ featureview.name }}__base
    GROUP BY {{featureview.name}}__entity_row_unique_id, event_timestamp
),
{% endif %}

/*
 3. The data has been filtered during the first CTE "*__base"
 Thus we only need to compute the latest timestamp of each feature.
*/
{{ featureview.name }}__latest AS (
    SELECT
        base.{{featureview.name}}__entity_row_unique_id,
        MAX(base.event_timestamp) AS event_timestamp
        {% if featureview.created_timestamp_column %}
            ,MAX(base.created_timestamp) AS created_timestamp
        {% endif %}

    FROM {{ featureview.name }}__base AS base
    {% if featureview.created_timestamp_column %}
        INNER JOIN {{ featureview.name }}__dedup AS dedup
        ON (
            dedup.{{featureview.name}}__entity_row_unique_id=base.{{featureview.name}}__entity_row_unique_id
            AND dedup.event_timestamp=base.event_timestamp
            AND dedup.created_timestamp=base.created_timestamp
        )
    {% endif %}

    GROUP BY base.{{featureview.name}}__entity_row_unique_id
),

/*
 4. Once we know the latest value of each feature for a given timestamp,
 we can join again the data back to the original "base" dataset
*/
{{ featureview.name }}__cleaned AS (
    SELECT base.*
    FROM {{ featureview.name }}__base AS base
    INNER JOIN {{ featureview.name }}__latest AS latest
    ON (
        base.{{featureview.name}}__entity_row_unique_id=latest.{{featureview.name}}__entity_row_unique_id
        AND base.event_timestamp=latest.event_timestamp
        {% if featureview.created_timestamp_column %}
            AND base.created_timestamp=latest.created_timestamp
        {% endif %}
    )
){% if loop.last %}{% else %}, {% endif %}


{% endfor %}

/*
 Joins the outputs of multiple time travel joins to a single table.
 The entity_dataframe dataset being our source of truth here.
 */

SELECT `(entity_timestamp|{% for featureview in featureviews %}{{featureview.name}}__entity_row_unique_id{% if loop.last %}{% else %}|{% endif %}{% endfor %})?+.+`
FROM entity_dataframe
{% for featureview in featureviews %}
LEFT JOIN (
    SELECT
        {{featureview.name}}__entity_row_unique_id
        {% for feature in featureview.features %}
            ,{% if full_feature_names %}{{ featureview.name }}__{{feature}}{% else %}{{ feature }}{% endif %}
        {% endfor %}
    FROM {{ featureview.name }}__cleaned
) AS {{ featureview.name }}__joined
ON (
    {{ featureview.name }}__joined.{{featureview.name}}__entity_row_unique_id=entity_dataframe.{{featureview.name}}__entity_row_unique_id
)
{% endfor %}"""
