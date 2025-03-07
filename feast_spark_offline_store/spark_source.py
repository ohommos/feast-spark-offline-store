from typing import Optional, Dict, Callable, Any, Tuple, Iterable
from feast.data_source import DataSource
from feast.repo_config import RepoConfig
from pyspark.sql.utils import AnalysisException
from feast.value_type import ValueType
from feast.protos.feast.core.DataSource_pb2 import DataSource as DataSourceProto
from feast_spark_offline_store.spark_type_map import spark_to_feast_value_type
import pickle
from feast.errors import DataSourceNotFoundException


class SparkSource(DataSource):
    def __init__(
        self,
        table: Optional[str] = None,
        query: Optional[str] = None,
        # TODO support file readers
        # path: Optional[str] = None,
        # jdbc=None,
        # format: Optional[str] = None,
        # options: Optional[Dict[str, Any]] = None,
        event_timestamp_column: Optional[str] = None,
        created_timestamp_column: Optional[str] = None,
        field_mapping: Optional[Dict[str, str]] = None,
        date_partition_column: Optional[str] = None,
    ):
        super().__init__(
            event_timestamp_column,
            created_timestamp_column,
            field_mapping,
            date_partition_column,
        )

        self._spark_options = SparkOptions(
            table=table,
            query=query,
            # path=path,
            # jdbc=None,
            # format=format,
            # options=options,
        )

    @property
    def spark_options(self):
        """
        Returns the spark options of this data source
        """
        return self._spark_options

    @spark_options.setter
    def spark_options(self, spark_options):
        """
        Sets the spark options of this data source
        """
        self._spark_options = spark_options

    @property
    def table(self):
        """
        Returns the table of this feature data source
        """
        return self._spark_options.table

    @property
    def query(self):
        """
        Returns the query of this feature data source
        """
        return self._spark_options.query

    @staticmethod
    def from_proto(data_source: DataSourceProto) -> Any:

        assert data_source.HasField("custom_options")

        spark_options = SparkOptions.from_proto(data_source.custom_options)

        return SparkSource(
            field_mapping=dict(data_source.field_mapping),
            table=spark_options.table,
            query=spark_options.query,
            # path=spark_options.path,
            # jdbc=None,
            # format=spark_options.format,
            # options=spark_options.options,
            event_timestamp_column=data_source.event_timestamp_column,
            created_timestamp_column=data_source.created_timestamp_column,
            date_partition_column=data_source.date_partition_column,
        )

    def to_proto(self) -> DataSourceProto:
        data_source_proto = DataSourceProto(
            type=DataSourceProto.CUSTOM_SOURCE,
            field_mapping=self.field_mapping,
            custom_options=self.spark_options.to_proto(),
        )

        data_source_proto.event_timestamp_column = self.event_timestamp_column
        data_source_proto.created_timestamp_column = self.created_timestamp_column
        data_source_proto.date_partition_column = self.date_partition_column

        return data_source_proto

    def validate(self, config: RepoConfig):
        self.get_table_column_names_and_types(config)

    @staticmethod
    def source_datatype_to_feast_value_type() -> Callable[[str], ValueType]:
        # TODO see feast.type_map for examples
        return spark_to_feast_value_type

    def get_table_column_names_and_types(
        self, config: RepoConfig
    ) -> Iterable[Tuple[str, str]]:
        from feast_spark_offline_store.spark import (
            get_spark_session_or_start_new_with_repoconfig,
        )

        spark_session = get_spark_session_or_start_new_with_repoconfig(
            config.offline_store
        )
        try:
            return (
                (fields["name"], fields["type"])
                for fields in spark_session.table(self.table).schema.jsonValue()[
                    "fields"
                ]
            )
        except AnalysisException:
            raise DataSourceNotFoundException(self.table)

    def get_table_query_string(self) -> str:
        """Returns a string that can directly be used to reference this table in SQL"""
        if self.table:
            return f"`{self.table}`"
        else:
            return f"({self.query})"


class SparkOptions:
    def __init__(
        self,
        table: str,
        query: str,
    ):
        self._table = table
        self._query = query

    @property
    def table(self):
        """
        Returns the table
        """
        return self._table

    @table.setter
    def table(self, table):
        """
        Sets the table
        """
        self._table = table

    @property
    def query(self):
        """
        Returns the query
        """
        return self._query

    @query.setter
    def query(self, query):
        """
        Sets the query
        """
        self._query = query

    @classmethod
    def from_proto(cls, spark_options_proto: DataSourceProto.CustomSourceOptions):
        """
        Creates a SparkOptions from a protobuf representation of a spark option

        args:
            spark_options_proto: a protobuf representation of a datasource

        Returns:
            Returns a SparkOptions object based on the spark_options protobuf
        """
        spark_configuration = pickle.loads(spark_options_proto.configuration)

        spark_options = cls(
            table=spark_configuration.table,
            query=spark_configuration.query,
        )
        return spark_options

    def to_proto(self) -> DataSourceProto.CustomSourceOptions:
        """
        Converts an SparkOptionsProto object to its protobuf representation.

        Returns:
            SparkOptionsProto protobuf
        """

        spark_options_proto = DataSourceProto.CustomSourceOptions(
            configuration=pickle.dumps(self),
        )

        return spark_options_proto
