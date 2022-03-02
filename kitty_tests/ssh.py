#!/usr/bin/env python
# License: GPLv3 Copyright: 2021, Kovid Goyal <kovid at kovidgoyal.net>


import os
import shlex
import shutil
import tempfile
from functools import lru_cache

from kittens.ssh.config import load_config, options_for_host
from kittens.ssh.main import bootstrap_script, get_connection_data
from kittens.ssh.options.utils import DELETE_ENV_VAR
from kittens.transfer.utils import set_paths
from kitty.constants import is_macos
from kitty.fast_data_types import CURSOR_BEAM
from kitty.options.utils import shell_integration
from kitty.utils import SSHConnectionData

from . import BaseTest
from .shell_integration import bash_ok, basic_shell_env


def files_in(path):
    for record in os.walk(path):
        for f in record[-1]:
            yield os.path.relpath(os.path.join(record[0], f), path)


class SSHKitten(BaseTest):

    def test_basic_pty_operations(self):
        pty = self.create_pty('echo hello')
        pty.process_input_from_child()
        self.ae(pty.screen_contents(), 'hello')
        pty = self.create_pty(self.cmd_to_run_python_code('''\
import array, fcntl, sys, termios
buf = array.array('H', [0, 0, 0, 0])
fcntl.ioctl(sys.stdout, termios.TIOCGWINSZ, buf)
print(' '.join(map(str, buf)))'''), lines=13, cols=77)
        pty.process_input_from_child()
        self.ae(pty.screen_contents(), '13 77 770 260')

    def test_ssh_connection_data(self):
        def t(cmdline, binary='ssh', host='main', port=None, identity_file=''):
            if identity_file:
                identity_file = os.path.abspath(identity_file)
            q = get_connection_data(cmdline.split())
            self.ae(q, SSHConnectionData(binary, host, port, identity_file))

        t('ssh main')
        t('ssh un@ip -i ident -p34', host='un@ip', port=34, identity_file='ident')
        t('ssh un@ip -iident -p34', host='un@ip', port=34, identity_file='ident')
        t('ssh -p 33 main', port=33)

    def test_ssh_config_parsing(self):
        def parse(conf):
            return load_config(overrides=conf.splitlines())

        def for_host(hostname, conf):
            if isinstance(conf, str):
                conf = parse(conf)
            return options_for_host(hostname, conf)

        self.ae(for_host('x', '').env, {})
        self.ae(for_host('x', 'env a=b').env, {'a': 'b'})
        pc = parse('env a=b\nhostname 2\nenv a=c\nenv b=b')
        self.ae(set(pc.keys()), {'*', '2'})
        self.ae(for_host('x', pc).env, {'a': 'b'})
        self.ae(for_host('2', pc).env, {'a': 'c', 'b': 'b'})
        self.ae(for_host('x', 'env a=').env, {'a': ''})
        self.ae(for_host('x', 'env a').env, {'a': '_delete_this_env_var_'})

    @property
    @lru_cache()
    def all_possible_sh(self):
        return tuple(sh for sh in ('dash', 'zsh', 'bash', 'posh', 'sh') if shutil.which(sh))

    def test_ssh_copy(self):
        simple_data = 'rkjlhfwf9whoaa'

        def touch(p):
            with open(os.path.join(local_home, p), 'w') as f:
                f.write(simple_data)

        for sh in self.all_possible_sh:
            with self.subTest(sh=sh), tempfile.TemporaryDirectory() as remote_home, tempfile.TemporaryDirectory() as local_home, set_paths(home=local_home):
                tuple(map(touch, 'simple-file g.1 g.2'.split()))
                os.makedirs(f'{local_home}/d1/d2/d3')
                touch('d1/d2/x')
                touch('d1/d2/w.exclude')
                os.symlink('d2/x', f'{local_home}/d1/y')

                conf = '''\
copy simple-file
copy --dest=a/sfa simple-file
copy --glob g.*
copy --exclude */w.* d1
'''
                copy = load_config(overrides=filter(None, conf.splitlines()))['*'].copy
                self.check_bootstrap(
                    sh, remote_home, extra_exec='env; exit 0', SHELL_INTEGRATION_VALUE='',
                    ssh_opts={'copy': copy}
                )
                self.assertTrue(os.path.lexists(f'{remote_home}/.terminfo/78'))
                self.assertTrue(os.path.exists(f'{remote_home}/.terminfo/78/xterm-kitty'))
                self.assertTrue(os.path.exists(f'{remote_home}/.terminfo/x/xterm-kitty'))
                for w in ('simple-file', 'a/sfa'):
                    with open(os.path.join(remote_home, w), 'r') as f:
                        self.ae(f.read(), simple_data)
                self.assertTrue(os.path.lexists(f'{remote_home}/d1/y'))
                self.assertTrue(os.path.exists(f'{remote_home}/d1/y'))
                self.ae(os.readlink(f'{remote_home}/d1/y'), 'd2/x')
                contents = set(files_in(remote_home))
                contents.discard('.zshrc')  # added by check_bootstrap()
                self.ae(contents, {
                    'g.1', 'g.2', '.terminfo/kitty.terminfo', 'simple-file', '.terminfo/x/xterm-kitty', 'd1/d2/x', 'd1/y',
                    'a/sfa'
                })

    def test_ssh_env_vars(self):
        for sh in self.all_possible_sh:
            with self.subTest(sh=sh), tempfile.TemporaryDirectory() as tdir:
                pty = self.check_bootstrap(
                    sh, tdir, extra_exec='env; exit 0', SHELL_INTEGRATION_VALUE='',
                    ssh_opts={'env': {
                        'TSET': 'set-works',
                        'COLORTERM': DELETE_ENV_VAR,
                    }}
                )
                pty.wait_till(lambda: 'TSET=set-works' in pty.screen_contents())
                self.assertNotIn('COLORTERM', pty.screen_contents())

    def test_ssh_leading_data(self):
        for sh in self.all_possible_sh:
            with self.subTest(sh=sh), tempfile.TemporaryDirectory() as tdir:
                pty = self.check_bootstrap(
                    sh, tdir, extra_exec='echo "ld:$leading_data"; exit 0',
                    SHELL_INTEGRATION_VALUE='', pre_data='before_tarfile')
                self.ae(pty.screen_contents(), 'UNTAR_DONE\nld:before_tarfile')

    def test_ssh_login_shell_detection(self):
        methods = []
        if shutil.which('python') or shutil.which('python3') or shutil.which('python2'):
            methods.append('using_python')
        if is_macos:
            methods += ['using_id']
        else:
            if shutil.which('getent'):
                methods.append('using_getent')
            if os.access('/etc/passwd', os.R_OK):
                methods.append('using_passwd')
        self.assertTrue(methods)
        import pwd
        expected_login_shell = pwd.getpwuid(os.geteuid()).pw_shell
        for m in methods:
            for sh in self.all_possible_sh:
                with self.subTest(sh=sh, method=m), tempfile.TemporaryDirectory() as tdir:
                    pty = self.check_bootstrap(sh, tdir, extra_exec=f'{m}; echo "$login_shell"; exit 0', SHELL_INTEGRATION_VALUE='')
                    self.assertIn(expected_login_shell, pty.screen_contents())

    def test_ssh_shell_integration(self):
        ok_login_shell = ''
        for sh in self.all_possible_sh:
            for login_shell in {'fish', 'zsh', 'bash'} & set(self.all_possible_sh):
                if login_shell == 'bash' and not bash_ok():
                    continue
                ok_login_shell = login_shell
                with self.subTest(sh=sh, login_shell=login_shell), tempfile.TemporaryDirectory() as tdir:
                    pty = self.check_bootstrap(sh, tdir, login_shell)
                    if login_shell == 'bash':
                        pty.send_cmd_to_child('echo $HISTFILE')
                        pty.wait_till(lambda: '/.bash_history' in pty.screen_contents())
        # check that turning off shell integration works
        if ok_login_shell in ('bash', 'zsh'):
            for val in ('', 'no-rc', 'enabled no-rc'):
                with tempfile.TemporaryDirectory() as tdir:
                    self.check_bootstrap('sh', tdir, ok_login_shell, val)

    def check_bootstrap(self, sh, home_dir, login_shell='', SHELL_INTEGRATION_VALUE='enabled', extra_exec='', pre_data='', ssh_opts=None):
        script = bootstrap_script(
            EXEC_CMD=f'echo "UNTAR_DONE"; {extra_exec}',
            OVERRIDE_LOGIN_SHELL=login_shell,
        )
        env = basic_shell_env(home_dir)
        # Avoid generating unneeded completion scripts
        os.makedirs(os.path.join(home_dir, '.local', 'share', 'fish', 'generated_completions'), exist_ok=True)
        # prevent newuser-install from running
        open(os.path.join(home_dir, '.zshrc'), 'w').close()
        options = {'shell_integration': shell_integration(SHELL_INTEGRATION_VALUE or 'disabled')}
        pty = self.create_pty(f'{sh} -c {shlex.quote(script)}', cwd=home_dir, env=env, options=options, ssh_opts=ssh_opts)
        if pre_data:
            pty.write_buf = pre_data.encode('utf-8')
        del script

        def check_untar_or_fail():
            q = pty.screen_contents()
            if 'bzip2' in q:
                raise ValueError('Untarring failed with screen contents:\n' + q)
            return 'UNTAR_DONE' in q
        pty.wait_till(check_untar_or_fail)
        self.assertTrue(os.path.exists(os.path.join(home_dir, '.terminfo/kitty.terminfo')))
        if SHELL_INTEGRATION_VALUE != 'enabled':
            pty.wait_till(lambda: len(pty.screen_contents().splitlines()) > 1)
            self.assertEqual(pty.screen.cursor.shape, 0)
        else:
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
        return pty
