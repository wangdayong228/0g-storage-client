import argparse
from enum import Enum
import logging
import os
import pdb
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import json
from pathlib import Path

from eth_utils import encode_hex
from test_framework.contract_proxy import FlowContractProxy, MineContractProxy, IRewardContractProxy
from test_framework.zgs_node import ZgsNode
from test_framework.blockchain_node import BlockChainNodeType
from test_framework.zg_node import ZGNode, zg_node_init_genesis
from test_framework.kv_node import KVNode
from utility.utils import PortMin, is_windows_platform, wait_until, indexer_port
from utility.build_binary import build_zgs, build_kv

__file_path__ = os.path.dirname(os.path.realpath(__file__))


class TestStatus(Enum):
    PASSED = 1
    FAILED = 2


TEST_EXIT_PASSED = 0
TEST_EXIT_FAILED = 1


class TestFramework:
    def __init__(self, blockchain_node_type=BlockChainNodeType.ZG):
        if "http_proxy" in os.environ:
            del os.environ["http_proxy"]

        self.num_blockchain_nodes = None
        self.num_nodes = None
        self.blockchain_nodes = []
        self.nodes = []
        self.kv_nodes = []
        self.contract = None
        self.blockchain_node_configs = {}
        self.zgs_node_configs = {}
        self.blockchain_node_type = blockchain_node_type
        self.block_time = blockchain_node_type.block_time()
        self.enable_market = False
        self.mine_period = 100
        self.launch_wait_seconds = 1

        self.indexer_process = None
        self.indexer_rpc_url = None

        # Set default binary path
        binary_ext = ".exe" if is_windows_platform() else ""
        tests_dir = os.path.dirname(__file_path__)
        root_dir = os.path.dirname(tests_dir)
        self.__default_geth_binary__ = os.path.join(
            tests_dir, "tmp", "geth" + binary_ext
        )
        self.__default_zg_binary__ = os.path.join(
            tests_dir, "tmp", "0gchaind" + binary_ext
        )
        self.__default_zgs_node_binary__ = os.path.join(
            tests_dir, "tmp", "zgs_node" + binary_ext
        )
        self.__default_zgs_cli_binary__ = os.path.join(
            root_dir, "0g-storage-client"  + binary_ext
        )
        self.__default_zgs_kv_binary__ = os.path.join(
            tests_dir, "tmp", "zgs_kv" + binary_ext
        )

    def __setup_blockchain_node(self):
        if self.blockchain_node_type == BlockChainNodeType.ZG:
            zg_node_init_genesis(self.blockchain_binary, self.root_dir, self.num_blockchain_nodes)
            self.log.info("0gchain genesis initialized for %s nodes" % self.num_blockchain_nodes)

        for i in range(self.num_blockchain_nodes):
            if i in self.blockchain_node_configs:
                updated_config = self.blockchain_node_configs[i]
            else:
                updated_config = {}

            node = None
            if self.blockchain_node_type == BlockChainNodeType.ZG:
                node = ZGNode(
                    i,
                    self.root_dir,
                    self.blockchain_binary,
                    updated_config,
                    self.contract_path,
                    self.log,
                )
            else:
                raise NotImplementedError

            self.blockchain_nodes.append(node)
            node.setup_config()
            node.start()

        # wait node to start to avoid NewConnectionError
        time.sleep(1)
        for node in self.blockchain_nodes:
            node.wait_for_rpc_connection()

        if self.blockchain_node_type == BlockChainNodeType.ZG:
            # wait for the first block
            self.log.debug("Wait 3 seconds for 0gchain node to generate first block")
            time.sleep(3)
            for node in self.blockchain_nodes:
                wait_until(lambda: node.net_peerCount() == self.num_blockchain_nodes - 1)
                wait_until(lambda: node.eth_blockNumber() is not None)
                wait_until(lambda: int(node.eth_blockNumber(), 16) > 0)

        contract, tx_hash, mine_contract, reward_contract = self.blockchain_nodes[0].setup_contract(self.enable_market, self.mine_period)
        self.contract = FlowContractProxy(contract, self.blockchain_nodes)
        self.mine_contract = MineContractProxy(mine_contract, self.blockchain_nodes)
        self.reward_contract = IRewardContractProxy(reward_contract, self.blockchain_nodes)


        for node in self.blockchain_nodes[1:]:
            node.wait_for_transaction(tx_hash)

    def __setup_zgs_node(self):
        build_zgs(Path(self.zgs_binary).parent.absolute())
        for i in range(self.num_nodes):
            if i in self.zgs_node_configs:
                updated_config = self.zgs_node_configs[i]
            else:
                updated_config = {}

            assert os.path.exists(self.zgs_binary), (
                "%s should be exist" % self.zgs_binary
            )
            node = ZgsNode(
                i,
                self.root_dir,
                self.zgs_binary,
                updated_config,
                self.contract.address(),
                self.mine_contract.address(),
                self.log,
            )
            self.nodes.append(node)
            node.setup_config()
            # wait first node start for connection
            if i > 0:
                time.sleep(1)
            node.start()

        self.log.info("Wait the zgs_node launch for %d seconds", self.launch_wait_seconds)
        time.sleep(self.launch_wait_seconds)
        
        for node in self.nodes:
            node.wait_for_rpc_connection()

    def add_arguments(self, parser: argparse.ArgumentParser):

        parser.add_argument(
            "--zg-binary",
            dest="zg",
            default=self.__default_zg_binary__,
            type=str,
        )

        parser.add_argument(
            "--zerog-storage-binary",
            dest="zerog_storage",
            default=os.getenv(
                "ZGS",
                default=self.__default_zgs_node_binary__,
            ),
            type=str,
        )

        parser.add_argument(
            "--zerog-storage-client",
            dest="cli",
            default=self.__default_zgs_cli_binary__,
            type=str,
        )

        parser.add_argument(
            "--zgs-kv",
            dest="zgs_kv",
            default=self.__default_zgs_kv_binary__,
            type=str,
        )

        parser.add_argument(
            "--contract-path",
            dest="contract",
            default=os.path.join(
                __file_path__,
                "../storage-contracts-abis/",
            ),
            type=str,
        )

        parser.add_argument(
            "-l",
            "--loglevel",
            dest="loglevel",
            default="INFO",
            help="log events at this level and higher to the console. Can be set to DEBUG, INFO, WARNING, ERROR or CRITICAL. Passing --loglevel DEBUG will output all logs to console. Note that logs at all levels are always written to the test_framework.log file in the temporary test directory.",
        )

        parser.add_argument(
            "--tmpdir", dest="tmpdir", help="Root directory for datadirs"
        )

        parser.add_argument(
            "--devdir", dest="devdir", help="A softlink point to the last run"
        )

        parser.add_argument(
            "--randomseed", dest="random_seed", type=int, help="Set a random seed"
        )

        parser.add_argument("--port-min", dest="port_min", default=11000, type=int)

        parser.add_argument(
            "--pdbonfailure",
            dest="pdbonfailure",
            default=False,
            action="store_true",
            help="Attach a python debugger if test fails",
        )

    def __start_logging(self):
        # Add logger and logging handlers
        self.log = logging.getLogger("TestFramework")
        self.log.setLevel(logging.DEBUG)

        # Create file handler to log all messages
        fh = logging.FileHandler(
            self.options.tmpdir + "/test_framework.log", encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)

        # Create console handler to log messages to stderr. By default this logs only error messages, but can be configured with --loglevel.
        ch = logging.StreamHandler(sys.stdout)
        # User can provide log level as a number or string (eg DEBUG). loglevel was caught as a string, so try to convert it to an int
        ll = (
            int(self.options.loglevel)
            if self.options.loglevel.isdigit()
            else self.options.loglevel.upper()
        )
        ch.setLevel(ll)

        # Format logs the same as bitcoind's debug.log with microprecision (so log files can be concatenated and sorted)
        formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d000Z %(name)s (%(levelname)s): %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)

        # add the handlers to the logger
        self.log.addHandler(fh)
        self.log.addHandler(ch)

    def _check_cli_binary(self):
        assert os.path.exists(self.cli_binary), (
            "zgs CLI binary not found: %s" % self.cli_binary
        )

    def _upload_file_use_cli(
        self,
        blockchain_node_rpc_url,
        contract_address,
        key,
        node_rpc_url,
        indexer_url,
        file_to_upload,
        skip_tx = True,
    ):
        upload_args = [
            self.cli_binary,
            "upload",
            "--url",
            blockchain_node_rpc_url,
            "--contract",
            contract_address,
            "--key",
            encode_hex(key),
            "--skip-tx="+str(skip_tx),
            "--log-level",
            "debug",
            "--gas-limit",
            "10000000",
        ]
        if node_rpc_url is not None:
            upload_args.append("--node")
            upload_args.append(node_rpc_url)
        elif indexer_url is not None:
            upload_args.append("--indexer")
            upload_args.append(indexer_url)
        upload_args.append("--file")
        self.log.info("upload file with cli: {}".format(upload_args))

        output = tempfile.NamedTemporaryFile(dir=self.root_dir, delete=False, prefix="zgs_client_output_")
        output_name = output.name
        output_fileno = output.fileno()

        try:
            proc = subprocess.Popen(
                upload_args + [file_to_upload.name],
                text=True,
                stdout=output_fileno,
                stderr=output_fileno,
            )
            
            return_code = proc.wait(timeout=60)

            output.seek(0)
            lines = output.readlines()
            for line in lines:
                line = line.decode("utf-8")
                self.log.debug("line: %s", line)
                if "root" in line:
                    filtered_line = re.sub(r'\x1b\[([0-9,A-Z]{1,2}(;[0-9]{1,2})?(;[0-9]{3})?)?[m|K]?', '', line)
                    index = filtered_line.find("root=")
                    if index > 0:
                        root = filtered_line[index + 5 : index + 5 + 66]
        except Exception as ex:
            self.log.error("Failed to upload file via CLI tool, output: %s", output_name)
            raise ex
        finally:
            output.close()

        assert return_code == 0, "%s upload file failed, output: %s, log: %s" % (self.cli_binary, output_name, lines)

        return root
    
    def _download_file_use_cli(
        self,
        node_rpc_url,
        indexer_url,
        root,
        with_proof = True,
    ):
        file_to_download = os.path.join(self.root_dir, "download_{}_{}".format(root, time.time()))
        download_args = [
            self.cli_binary,
            "download",
            "--file",
            file_to_download,
            "--root",
            root,
            "--proof=" + str(with_proof),
            "--log-level",
            "debug",
        ]
        if node_rpc_url is not None:
            download_args.append("--node")
            download_args.append(node_rpc_url)
        elif indexer_url is not None:
            download_args.append("--indexer")
            download_args.append(indexer_url)
        self.log.info("download file with cli: {}".format(download_args))

        output = tempfile.NamedTemporaryFile(dir=self.root_dir, delete=False, prefix="zgs_client_output_")
        output_name = output.name
        output_fileno = output.fileno()

        try:
            proc = subprocess.Popen(
                download_args,
                text=True,
                stdout=output_fileno,
                stderr=output_fileno,
            )
            
            return_code = proc.wait(timeout=60)
            output.seek(0)
            lines = output.readlines()
        except Exception as ex:
            self.log.error("Failed to download file via CLI tool, output: %s", output_name)
            raise ex
        finally:
            output.close()

        assert return_code == 0, "%s download file failed, output: %s, log: %s" % (self.cli_binary, output_name, lines)

        os.remove(file_to_download)

        return
    
    def _kv_write_use_cli(
        self,
        blockchain_node_rpc_url,
        contract_address,
        key,
        node_rpc_url,
        indexer_url,
        stream_id,
        kv_keys,
        kv_values,
        skip_tx = True,
    ):
        kv_write_args = [
            self.cli_binary,
            "kv-write",
            "--url",
            blockchain_node_rpc_url,
            "--contract",
            contract_address,
            "--key",
            encode_hex(key),
            "--skip-tx="+str(skip_tx),
            "--stream-id",
            stream_id,
            "--stream-keys",
            kv_keys,
            "--stream-values",
            kv_values,
            "--log-level",
            "debug",
            "--gas-limit",
            "10000000",
        ]
        if node_rpc_url is not None:
            kv_write_args.append("--node")
            kv_write_args.append(node_rpc_url)
        elif indexer_url is not None:
            kv_write_args.append("--indexer")
            kv_write_args.append(indexer_url)
        self.log.info("kv write with cli: {}".format(kv_write_args))

        output = tempfile.NamedTemporaryFile(dir=self.root_dir, delete=False, prefix="zgs_client_output_")
        output_name = output.name
        output_fileno = output.fileno()

        try:
            proc = subprocess.Popen(
                kv_write_args,
                text=True,
                stdout=output_fileno,
                stderr=output_fileno,
            )
            
            return_code = proc.wait(timeout=60)

            output.seek(0)
            lines = output.readlines()
        except Exception as ex:
            self.log.error("Failed to write kv via CLI tool, output: %s", output_name)
            raise ex
        finally:
            output.close()

        assert return_code == 0, "%s write kv failed, output: %s, log: %s" % (self.cli_binary, output_name, lines)

        return

    def _kv_read_use_cli(
        self,
        node_rpc_url,
        stream_id,
        kv_keys
    ):
        kv_read_args = [
            self.cli_binary,
            "kv-read",
            "--node",
            node_rpc_url,
            "--stream-id",
            stream_id,
            "--stream-keys",
            kv_keys,
            "--log-level",
            "debug",
        ]
        self.log.info("kv read with cli: {}".format(kv_read_args))

        output = tempfile.NamedTemporaryFile(dir=self.root_dir, delete=False, prefix="zgs_client_output_")
        output_name = output.name
        output_fileno = output.fileno()

        try:
            proc = subprocess.Popen(
                kv_read_args,
                text=True,
                stdout=output_fileno,
                stderr=output_fileno,
            )
            
            return_code = proc.wait(timeout=60)
            output.seek(0)
            lines = output.readlines()
        except Exception as ex:
            self.log.error("Failed to read kv via CLI tool, output: %s", output_name)
            raise ex
        finally:
            output.close()

        assert return_code == 0, "%s read kv failed, output: %s, log: %s" % (self.cli_binary, output_name, lines)

        return json.loads(lines[0].decode("utf-8").strip())

    def setup_params(self):
        self.num_blockchain_nodes = 1
        self.num_nodes = 1

    def setup_nodes(self):
        self.__setup_blockchain_node()
        self.__setup_zgs_node()

    def setup_kv_node(self, index, stream_ids, updated_config={}):
        build_kv(Path(self.kv_binary).parent.absolute())
        assert os.path.exists(self.kv_binary), "%s should be exist" % self.kv_binary
        node = KVNode(
            index,
            self.root_dir,
            self.kv_binary,
            updated_config,
            self.contract.address(),
            self.log,
            stream_ids=stream_ids,
        )
        self.kv_nodes.append(node)
        node.setup_config()
        node.start()

        time.sleep(1)
        node.wait_for_rpc_connection()
    
    def setup_indexer(self, trusted, discover_node, discover_ports = None):
        indexer_args = [
            self.cli_binary,
            "indexer",
            "--endpoint",
            ":{}".format(indexer_port(0)),
            "--trusted",
            trusted,
            "--log-level",
            "debug",
        ]
        if discover_ports is not None:
            indexer_args.append("--discover-ports")
            indexer_args.append(discover_ports)
        if discover_node is not None:
            indexer_args.append("--node")
            indexer_args.append(discover_node)
        self.log.info("start indexer with args: {}".format(indexer_args))
        data_dir = os.path.join(self.root_dir, "indexer0")
        os.mkdir(data_dir)
        stdout = tempfile.NamedTemporaryFile(
            dir=data_dir, prefix="stdout", delete=False
        )
        stderr = tempfile.NamedTemporaryFile(
            dir=data_dir, prefix="stderr", delete=False
        )
        self.indexer_process = subprocess.Popen(
            indexer_args,
            stdout=stdout,
            stderr=stderr,
            cwd=data_dir,
            env=os.environ.copy(),
        )
        self.indexer_rpc_url = "http://127.0.0.1:{}".format(indexer_port(0))
    
    def stop_indexer(self):
        if self.indexer_process is not None:
            self.indexer_process.terminate()
            wait_until(lambda: self.indexer_process.poll() is not None, timeout=20)

    def stop_nodes(self):
        # stop storage nodes first
        for node in self.nodes:
            node.stop()

        for node in self.blockchain_nodes:
            node.stop()

        for node in self.kv_nodes:
            node.stop()
        
        self.stop_indexer()

    def stop_kv_node(self, index):
        self.kv_nodes[index].stop()

    def stop_storage_node(self, index, clean=False):
        self.nodes[index].stop()
        if clean:
            self.nodes[index].clean_data()

    def start_kv_node(self, index):
        self.kv_nodes[index].start()

    def start_storage_node(self, index):
        self.nodes[index].start()

    def run_test(self):
        raise NotImplementedError

    def main(self):
        parser = argparse.ArgumentParser(usage="%(prog)s [options]")
        self.add_arguments(parser)
        self.options = parser.parse_args()
        PortMin.n = self.options.port_min

        # Set up temp directory and start logging
        if self.options.tmpdir:
            self.options.tmpdir = os.path.abspath(self.options.tmpdir)
            os.makedirs(self.options.tmpdir, exist_ok=True)
        else:
            self.options.tmpdir = os.getenv(
                "ZG_CLIENT_TESTS_LOG_DIR", default=tempfile.mkdtemp(prefix="zg_client_test_")
            )

        self.root_dir = self.options.tmpdir

        self.__start_logging()
        self.log.info("Root dir: %s", self.root_dir)

        if self.options.devdir:
            dst = self.options.devdir

            if os.path.islink(dst):
                os.remove(dst)
            elif os.path.isdir(dst): 
                shutil.rmtree(dst)
            elif os.path.exists(dst):
                os.remove(dst)

            os.symlink(self.options.tmpdir, dst)
            self.log.info("Symlink: %s", Path(dst).absolute())

        if self.blockchain_node_type == BlockChainNodeType.ZG:
            self.blockchain_binary = os.path.abspath(self.options.zg)
        else:
            raise NotImplementedError

        self.zgs_binary = os.path.abspath(self.options.zerog_storage)
        self.cli_binary = os.path.abspath(self.options.cli)
        self.contract_path = os.path.abspath(self.options.contract)
        self.kv_binary = os.path.abspath(self.options.zgs_kv)

        assert os.path.exists(self.contract_path), (
            "%s should be exist" % self.contract_path
        )

        if self.options.random_seed is not None:
            random.seed(self.options.random_seed)

        success = TestStatus.FAILED
        try:
            self.setup_params()
            self.setup_nodes()
            self.log.debug("========== start to run tests ==========")
            self.run_test()
            success = TestStatus.PASSED
        except AssertionError as e:
            self.log.exception("Assertion failed %s", repr(e))
        except KeyboardInterrupt as e:
            self.log.warning("Exiting after keyboard interrupt %s", repr(e))
        except Exception as e:
            self.log.error("Test exception %s %s", repr(e), traceback.format_exc())
            self.log.error(f"Test data are not deleted: {self.root_dir}")

        if success == TestStatus.FAILED and self.options.pdbonfailure:
            print("Testcase failed. Attaching python debugger. Enter ? for help")
            pdb.set_trace()

        if success == TestStatus.PASSED:
            self.log.info("Tests successful")
            exit_code = TEST_EXIT_PASSED
        else:
            self.log.error(
                "Test failed. Test logging available at %s/test_framework.log",
                self.options.tmpdir,
            )
            exit_code = TEST_EXIT_FAILED

        self.stop_nodes()

        handlers = self.log.handlers[:]
        for handler in handlers:
            self.log.removeHandler(handler)
            handler.close()
        logging.shutdown()

        if success == TestStatus.PASSED:
            shutil.rmtree(self.root_dir)

        sys.exit(exit_code)
