"""Owner-configured startup and secret boundaries; all clients are offline fakes."""
import contextlib
import io
from getpass import GetPassWarning
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import operations_workspace as ops
import operations_server as server


class ProviderSetupTests(unittest.TestCase):
    def test_unconfigured_auto_start_never_prompts_or_constructs_clients(self):
        with mock.patch.object(ops, 'getpass') as prompt, mock.patch.object(ops, 'RoleClient') as role:
            self.assertEqual(ops.browser_clients(environment={}), (None, None))
            prompt.assert_not_called();role.assert_not_called()

    def test_auto_start_constructs_three_owner_funded_clients_without_requests(self):
        with mock.patch.object(ops, 'getpass') as prompt, mock.patch.object(ops, 'RoleClient') as role:
            task, providers = ops.browser_clients(environment={'NEBIUS_API_KEY':'owner-fixture-value'})
            self.assertIsNone(task);self.assertEqual(set(providers), set(ops.ROLES))
            self.assertEqual(role.call_count, 3);prompt.assert_not_called()
            self.assertEqual(role.call_args_list, [mock.call(r, 'owner-fixture-value', 8192, True) for r in ops.ROLES])
            role.return_value.ask.assert_not_called()

    def test_forced_scripted_start_does_not_even_read_owner_credentials(self):
        environment = mock.Mock();environment.get.side_effect = AssertionError('Must not read credentials.')
        with mock.patch.object(ops, 'RoleClient') as role, mock.patch.object(ops, 'getpass') as prompt:
            self.assertEqual(ops.browser_clients('scripted', environment=environment), (None, None))
            environment.get.assert_not_called();role.assert_not_called();prompt.assert_not_called()

    def test_configured_live_start_works_without_an_interactive_terminal(self):
        with mock.patch.object(ops.m.terminal_guard, 'interactive_input', return_value=False), mock.patch.object(ops, 'RoleClient') as role, mock.patch.object(ops, 'getpass') as prompt:
            _, providers = ops.browser_clients('live', environment={'NEBIUS_API_KEY':'owner-fixture-value'})
            self.assertEqual(len(providers), 3);prompt.assert_not_called();role.return_value.ask.assert_not_called()

    def test_unconfigured_noninteractive_live_start_never_asks_judges_for_a_key(self):
        with mock.patch.object(ops.m.terminal_guard, 'interactive_input', return_value=False), mock.patch.object(ops, 'getpass') as prompt, mock.patch.object(ops, 'RoleClient') as role:
            with self.assertRaisesRegex(ValueError, 'Judges do not enter a key'):
                ops.browser_clients('live', environment={})
            prompt.assert_not_called();role.assert_not_called()

    def test_explicit_owner_command_may_use_one_hidden_prompt(self):
        with mock.patch.object(ops.m.terminal_guard, 'interactive_input', return_value=True), mock.patch.object(ops, 'getpass', return_value=' owner-fixture-value ') as prompt, mock.patch.object(ops, 'RoleClient') as role:
            ops.browser_clients('live', environment={})
            prompt.assert_called_once();self.assertEqual(role.call_count, 3)
            self.assertEqual(role.call_args.args[1], 'owner-fixture-value')

    def test_owner_prompt_refuses_echo_fallback(self):
        with mock.patch.object(ops.m.terminal_guard, 'interactive_input', return_value=True), mock.patch.object(ops, 'getpass', side_effect=GetPassWarning('No echo control')), mock.patch.object(ops, 'RoleClient') as role:
            with self.assertRaisesRegex(ValueError, 'hidden owner key prompt is unavailable'):
                ops.browser_clients('live', environment={})
            role.assert_not_called()

    def test_owner_prompt_eof_does_not_start_clients(self):
        with mock.patch.object(ops.m.terminal_guard, 'interactive_input', return_value=True), mock.patch.object(ops, 'getpass', side_effect=EOFError), mock.patch.object(ops, 'RoleClient') as role:
            with self.assertRaises(ValueError):ops.browser_clients('live', environment={})
            role.assert_not_called()

    def test_empty_owner_prompt_does_not_enable_live_mode(self):
        with mock.patch.object(ops.m.terminal_guard, 'interactive_input', return_value=True), mock.patch.object(ops, 'getpass', return_value=' '), mock.patch.object(ops, 'RoleClient') as role:
            with self.assertRaisesRegex(ValueError, 'No key supplied'):
                ops.browser_clients('live', environment={})
            role.assert_not_called()

    def test_invalid_mode_is_rejected_before_credentials_are_read(self):
        environment = mock.Mock()
        with self.assertRaises(ValueError):ops.browser_clients('unknown', environment=environment)
        environment.get.assert_not_called()

    def test_invalid_environment_value_is_rejected_without_printing_it(self):
        with self.assertRaisesRegex(ValueError, 'server environment'):
            ops.browser_clients(environment={'NEBIUS_API_KEY': {'private':'not-text'}})

    def test_provider_constructor_failure_never_exposes_its_private_message(self):
        with mock.patch.object(ops, 'RoleClient', side_effect=ValueError('PRIVATE_TEST_SENTINEL')):
            with self.assertRaises(ValueError) as caught:
                ops.browser_clients(environment={'NEBIUS_API_KEY':'owner-fixture-value'})
            self.assertNotIn('PRIVATE_TEST_SENTINEL', str(caught.exception))

    def test_live_roles_do_not_implicitly_enable_the_ticket_gateway(self):
        with mock.patch.object(ops, 'RoleClient'), mock.patch.object(ops.gateway, 'Nebius') as gateway:
            ops.browser_clients(environment={'NEBIUS_API_KEY':'owner-fixture-value'})
            gateway.assert_not_called()
            ops.browser_clients('scripted', enable_inference=True, environment={'NEBIUS_API_KEY':'owner-fixture-value'})
            gateway.assert_called_once_with('owner-fixture-value', 1024)

    def test_normal_cli_reuses_configured_key_and_makes_no_provider_request(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(ops.os.environ, {'NEBIUS_API_KEY':'owner-fixture-value'}, clear=True), mock.patch.object(ops, 'RoleClient') as role, mock.patch.object(ops, 'getpass') as prompt, mock.patch.object(server, 'serve') as listener, contextlib.redirect_stdout(output):
            self.assertEqual(ops.main(['serve', '--output', directory]), 0)
            prompt.assert_not_called();listener.assert_called_once();self.assertEqual(role.call_count, 3)
            role.return_value.ask.assert_not_called();self.assertNotIn('owner-fixture-value', output.getvalue())
            self.assertEqual(set(listener.call_args.args[5]), set(ops.ROLES))

    def test_cli_forced_scripted_remains_available_with_a_configured_key(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(ops.os.environ, {'NEBIUS_API_KEY':'owner-fixture-value'}, clear=True), mock.patch.object(ops, 'RoleClient') as role, mock.patch.object(server, 'serve') as listener, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ops.main(['serve', '--output', directory, '--proposal-mode', 'scripted']), 0)
            role.assert_not_called();self.assertIsNone(listener.call_args.args[5])


if __name__ == '__main__':unittest.main()
