import os
import shutil
import tempfile
import unittest
from typing import Type, Callable
from unittest.mock import patch

from ray_release.cluster_manager.cluster_manager import ClusterManager
from ray_release.cluster_manager.full import FullClusterManager
from ray_release.command_runner.command_runner import CommandRunner
from ray_release.config import Test
from ray_release.exception import (
    ReleaseTestConfigError,
    LocalEnvSetupError,
    ClusterComputeCreateError,
    ClusterEnvBuildError,
    ClusterEnvBuildTimeout,
    ClusterEnvCreateError,
    ClusterCreationError,
    ClusterStartupError,
    ClusterStartupTimeout,
)
from ray_release.file_manager.file_manager import FileManager
from ray_release.glue import (
    run_release_test,
    type_str_to_command_runner,
    command_runner_to_cluster_manager,
    command_runner_to_file_manager,
)
from ray_release.result import Result, ExitCode
from ray_release.tests.utils import MockSDK, APIDict


def _fail_on_call(error_type: Type[Exception] = RuntimeError, message: str = "Fail"):
    def _fail(*args, **kwargs):
        raise error_type(message)

    return _fail


class MockReturn:
    return_dict = {}

    def __getattribute__(self, item):
        return_dict = object.__getattribute__(self, "return_dict")
        if item in return_dict:
            mocked = return_dict[item]
            if isinstance(mocked, Callable):
                return mocked()
            else:
                return lambda *a, **kw: mocked
        return object.__getattribute__(self, item)


class EndToEndTest(unittest.TestCase):
    def writeClusterEnv(self, content: str):
        with open(os.path.join(self.tempdir, "cluster_env.yaml"), "wt") as fp:
            fp.write(content)

    def writeClusterCompute(self, content: str):
        with open(os.path.join(self.tempdir, "cluster_compute.yaml"), "wt") as fp:
            fp.write(content)

    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp()
        self.sdk = MockSDK()

        self.sdk.returns["get_project"] = APIDict(
            result=APIDict(name="unit_test_project")
        )

        self.writeClusterEnv("{'env': true}")
        self.writeClusterCompute("{'compute': true}")

        this_sdk = self.sdk
        this_tempdir = self.tempdir

        self.cluster_manager_return = {}
        self.command_runner_return = {}
        self.file_manager_return = {}

        this_cluster_manager_return = self.cluster_manager_return
        this_command_runner_return = self.command_runner_return
        this_file_manager_return = self.file_manager_return

        class MockClusterManager(MockReturn, FullClusterManager):
            def __init__(self, test_name: str, project_id: str, sdk=None):
                super(MockClusterManager, self).__init__(
                    test_name, project_id, this_sdk
                )
                self.return_dict = this_cluster_manager_return

        class MockCommandRunner(MockReturn, CommandRunner):
            return_dict = self.cluster_manager_return

            def __init__(
                self,
                cluster_manager: ClusterManager,
                file_manager: FileManager,
                working_dir: str,
            ):
                super(MockCommandRunner, self).__init__(
                    cluster_manager, file_manager, this_tempdir
                )
                self.return_dict = this_command_runner_return

        class MockFileManager(MockReturn, FileManager):
            def __init__(self, cluster_manager: ClusterManager):
                super(MockFileManager, self).__init__(cluster_manager)
                self.return_dict = this_file_manager_return

        type_str_to_command_runner["unit_test"] = MockCommandRunner
        command_runner_to_cluster_manager[MockCommandRunner] = MockClusterManager
        command_runner_to_file_manager[MockCommandRunner] = MockFileManager

        self.test = Test(
            name="unit_test_end_to_end",
            run=dict(type="unit_test"),
            working_dir=self.tempdir,
            cluster=dict(
                cluster_env="cluster_env.yaml", cluster_compute="cluster_compute.yaml"
            ),
        )
        self.anyscale_project = "prj_unit12345678"
        self.ray_wheels_url = "http://mock.wheels/"

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir)

    def _run(self, result: Result):
        run_release_test(
            test=self.test,
            anyscale_project=self.anyscale_project,
            result=result,
            ray_wheels_url=self.ray_wheels_url,
        )

    def testConfigInvalid(self):
        # Missing keys
        # Unknown command runner
        pass

    def testInvalidClusterEnv(self):
        result = Result()

        # Any ReleaseTestConfigError
        with patch(
            "ray_release.glue.load_test_cluster_env",
            _fail_on_call(ReleaseTestConfigError),
        ), self.assertRaises(ReleaseTestConfigError):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CONFIG_ERROR.value)

        # Fails because file not found
        os.unlink(os.path.join(self.tempdir, "cluster_env.yaml"))
        with self.assertRaisesRegex(ReleaseTestConfigError, "Path not found"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CONFIG_ERROR.value)

        # Fails because invalid jinja template
        self.writeClusterEnv("{{ INVALID")
        with self.assertRaisesRegex(ReleaseTestConfigError, "yaml template"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CONFIG_ERROR.value)

        # Fails because invalid json
        self.writeClusterEnv("{'test': true, 'fail}")
        with self.assertRaisesRegex(ReleaseTestConfigError, "quoted scalar"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CONFIG_ERROR.value)

    def testInvalidClusterCompute(self):
        result = Result()

        with patch(
            "ray_release.glue.load_test_cluster_compute",
            _fail_on_call(ReleaseTestConfigError),
        ), self.assertRaises(ReleaseTestConfigError):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CONFIG_ERROR.value)

        # Fails because file not found
        os.unlink(os.path.join(self.tempdir, "cluster_compute.yaml"))
        with self.assertRaisesRegex(ReleaseTestConfigError, "Path not found"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CONFIG_ERROR.value)

        # Fails because invalid jinja template
        self.writeClusterCompute("{{ INVALID")
        with self.assertRaisesRegex(ReleaseTestConfigError, "yaml template"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CONFIG_ERROR.value)

        # Fails because invalid json
        self.writeClusterCompute("{'test': true, 'fail}")
        with self.assertRaisesRegex(ReleaseTestConfigError, "quoted scalar"):
            self._run(result)

        self.assertEqual(result.return_code, ExitCode.CONFIG_ERROR.value)

    def testInvalidPrepareLocalEnv(self):
        result = Result()

        self.command_runner_return["prepare_local_env"] = _fail_on_call(
            LocalEnvSetupError
        )
        with self.assertRaises(LocalEnvSetupError):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.LOCAL_ENV_SETUP_ERROR.value)

    def testInvalidClusterIdOverride(self):
        # get_cluster_name() fails
        pass

    def testBuildConfigFailsClusterCompute(self):
        result = Result()

        # These commands should succeed
        self.command_runner_return["prepare_local_env"] = None

        # Fails because API response faulty
        with self.assertRaisesRegex(ClusterComputeCreateError, "Unexpected"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_RESOURCE_ERROR.value)

        # Fails for random cluster compute reason
        self.cluster_manager_return["create_cluster_compute"] = _fail_on_call(
            ClusterComputeCreateError, "Known"
        )
        with self.assertRaisesRegex(ClusterComputeCreateError, "Known"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_RESOURCE_ERROR.value)

    def testBuildConfigFailsClusterEnv(self):
        result = Result()

        # These commands should succeed
        self.command_runner_return["prepare_local_env"] = None
        self.cluster_manager_return["cluster_compute_id"] = "valid"
        self.cluster_manager_return["create_cluster_compute"] = None

        # Fails because API response faulty
        with self.assertRaisesRegex(ClusterEnvCreateError, "Unexpected"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_RESOURCE_ERROR.value)

        # Fails for random cluster env create reason
        self.cluster_manager_return["create_cluster_env"] = _fail_on_call(
            ClusterEnvCreateError, "Known"
        )
        with self.assertRaisesRegex(ClusterEnvCreateError, "Known"):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_RESOURCE_ERROR.value)

        # Now, succeed creation but fail on cluster env build
        self.cluster_manager_return["cluster_env_id"] = "valid"
        self.cluster_manager_return["create_cluster_env"] = None
        self.cluster_manager_return["build_cluster_env"] = _fail_on_call(
            ClusterEnvBuildError
        )
        with self.assertRaises(ClusterEnvBuildError):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_ENV_BUILD_ERROR.value)

        # Now, fail on cluster env timeout
        self.cluster_manager_return["build_cluster_env"] = _fail_on_call(
            ClusterEnvBuildTimeout
        )
        with self.assertRaises(ClusterEnvBuildTimeout):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_ENV_BUILD_TIMEOUT.value)

    def testStartClusterFails(self):
        result = Result()

        # These commands should succeed
        self.command_runner_return["prepare_local_env"] = None
        self.cluster_manager_return["cluster_compute_id"] = "valid"
        self.cluster_manager_return["create_cluster_compute"] = None
        self.cluster_manager_return["cluster_env_id"] = "valid"
        self.cluster_manager_return["create_cluster_env"] = None
        self.cluster_manager_return["cluster_env_build_id"] = "valid"
        self.cluster_manager_return["build_cluster_env"] = None

        # Fails because API response faulty
        with self.assertRaises(ClusterCreationError):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_RESOURCE_ERROR.value)

        # Fail for random cluster startup reason
        self.cluster_manager_return["start_cluster"] = _fail_on_call(
            ClusterStartupError
        )
        with self.assertRaises(ClusterStartupError):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_STARTUP_ERROR.value)

        # Fail for cluster startup timeout
        self.cluster_manager_return["start_cluster"] = _fail_on_call(
            ClusterStartupTimeout
        )
        with self.assertRaises(ClusterStartupTimeout):
            self._run(result)
        self.assertEqual(result.return_code, ExitCode.CLUSTER_STARTUP_TIMEOUT.value)

    def testPrepareRemoteEnvFails(self):
        pass

    def testPrepareCommandFails(self):
        pass

    def testPrepareCommandTimeout(self):
        pass

    def testTestCommandFails(self):
        pass

    def testTestCommandTimeout(self):
        pass

    def testFetchResultFails(self):
        pass

    def testLastLogsFails(self):
        pass

    def testAlertFails(self):
        pass

    def testReportFails(self):
        pass

    def testSuccessCaseOne(self):
        # New cluster compute
        # New cluster env
        # New cluster build
        # No smoke test
        pass

    def testSuccessCaseTwo(self):
        # Existing cluster compute
        # Existing cluster env
        # New cluster build
        # Smoke test
        pass

    def testSuccessCaseThree(self):
        # Existing cluster compute
        # Existing cluster env
        # Existing cluster build
        pass