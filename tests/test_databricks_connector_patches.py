from unittest import TestCase
from unittest.mock import Mock, patch

import pyarrow
from apollo.agent.proxy_client_factory import ProxyClientFactory
from databricks.sql import utils


class TestDatabricksConnectorPatches(TestCase):
    """
    hermes runs Databricks queries only through apollo. Build the client the way hermes does,
    through ProxyClientFactory, so this fails whenever such a client would fail duplicate-column
    queries with ArrowInvalid, whether apollo drops or moves its patch or the connector regresses.
    It checks behavior, not apollo internals, so it keeps passing if apollo retires the patch
    once the connector accepts duplicate column names upstream.
    """

    @patch("databricks.sql.connect", return_value=Mock())
    def test_databricks_client_tolerates_duplicate_column_names(self, connect: Mock):
        ProxyClientFactory.get_proxy_client(
            connection_type="databricks",
            credentials={
                "connect_args": {
                    "server_hostname": "example.cloud.databricks.com",
                    "http_path": "/sql/1.0/warehouses/abc",
                    "access_token": "token",
                }
            },
            skip_cache=True,
            platform="Generic",
        )

        connect.assert_called_once()
        table = pyarrow.table([[1], [2]], names=["id", "id"])
        try:
            result = utils._concat_arrow_tables([table])
        except pyarrow.ArrowInvalid:
            self.fail(
                "Databricks connector rejects duplicate column names; "
                "apollo's patch is not installed via ProxyClientFactory"
            )
        self.assertEqual(table, result)
