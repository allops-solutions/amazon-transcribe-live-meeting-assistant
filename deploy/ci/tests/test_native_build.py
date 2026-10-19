"""Exercise build architecture selection without Docker or network access."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / 'lma-ai-stack/deployment/configure-docker-multiarch.sh'


class NativeBuildTests(unittest.TestCase):
    def run_setup(self, system, architecture, docker_exit='0'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uname = root / 'uname'
            uname.write_text('#!/bin/sh\ncase "$1" in -s) echo "$TEST_SYSTEM";; -m) echo "$TEST_ARCH";; esac\n')
            uname.chmod(0o755)
            docker = root / 'docker'
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$TEST_DOCKER_LOG"\nexit "$TEST_DOCKER_EXIT"\n')
            docker.chmod(0o755)
            log = root / 'docker.log'
            result = subprocess.run(['bash', str(SCRIPT)], capture_output=True, text=True,
                                    env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                                         'TEST_SYSTEM': system, 'TEST_ARCH': architecture,
                                         'TEST_DOCKER_LOG': str(log), 'TEST_DOCKER_EXIT': docker_exit})
            return result, log.read_text() if log.exists() else ''

    def test_native_linux_never_pulls_qemu(self):
        for arch in ('x86_64', 'amd64'):
            with self.subTest(architecture=arch):
                result, docker = self.run_setup('Linux', arch)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(docker, '')
                self.assertIn('Native x86_64', result.stdout)

    def test_linux_arm_preserves_cross_architecture_setup(self):
        result, docker = self.run_setup('Linux', 'aarch64')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(docker.strip(), 'run --rm --privileged multiarch/qemu-user-static --reset -p yes')

    def test_macos_uses_docker_desktop_emulation(self):
        result, docker = self.run_setup('Darwin', 'arm64')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(docker, '')

    def test_required_emulator_failure_is_not_ignored(self):
        result, docker = self.run_setup('Linux', 'aarch64', docker_exit='125')
        self.assertEqual(result.returncode, 125)
        self.assertTrue(docker)


if __name__ == '__main__':
    unittest.main()
