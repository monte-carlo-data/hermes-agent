from unittest import TestCase
from unittest.mock import Mock, patch

import pyarrow
from apollo.agent.proxy_client_factory import ProxyClientFactory
from databricks.sql import utils

# Set by apollo's connector_patches on the wrapped databricks.sql.utils._concat_arrow_tables.
_PATCHED_ATTR = "_apollo_tolerates_duplicate_column_names"


class TestDatabricksConnectorPatches(TestCase):
    """
    hermes runs Databricks queries only through apollo, and the duplicate-column-name patch is
    installed when apollo's Databricks SQL warehouse client module is imported. Build the client
    the way hermes does, through ProxyClientFactory, so an apollo bump that drops or moves the
    patch fails here instead of failing customer queries with ArrowInvalid.
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
        self.assertTrue(getattr(utils._concat_arrow_tables, _PATCHED_ATTR, False))
        table = pyarrow.table([[1], [2]], names=["id", "id"])
        self.assertEqual(table, utils._concat_arrow_tables([table]))
