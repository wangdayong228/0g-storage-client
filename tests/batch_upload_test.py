import os
import time

from test_framework.test_framework import TestFramework
from config.node_config import GENESIS_PRIV_KEY
from utility.run_go_test import run_go_test

class BatchUploadTest(TestFramework):
    def setup_params(self):
        self.num_blockchain_nodes = 1
        self.num_nodes = 1

    def run_test(self):
        ports = ",".join([x.rpc_url.split(":")[-1] for x in self.nodes])
        self.setup_indexer(self.nodes[0].rpc_url, self.nodes[0].rpc_url, ports)
        test_args = [
            "go",
            "run",
            os.path.join(os.path.dirname(__file__), "go_tests", "batch_upload_test", "main.go"),
            # arguments passed to go
            GENESIS_PRIV_KEY,
            self.blockchain_nodes[0].rpc_url,
            ",".join([x.rpc_url for x in self.nodes]),
            self.indexer_rpc_url
        ]
        run_go_test(self.root_dir, test_args)
        

if __name__ == "__main__":
    BatchUploadTest().main()
