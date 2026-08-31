import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the server package on sys.path)

from micclaude import languages
from micclaude.cli import apply_overrides, build_parser
from micclaude.config import Config, ConfigError, TriggerConfig, apply_language, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


class PresetTests(unittest.TestCase):
    def test_english_and_russian_are_offered(self):
        self.assertEqual(sorted(languages.PRESETS), ["en", "ru"])

    def test_unknown_language_names_the_ones_we_have(self):
        with self.assertRaises(KeyError) as ctx:
            languages.get("kl")
        self.assertIn("en, ru", ctx.exception.args[0])

    def test_lookup_is_forgiving_about_case_and_space(self):
        self.assertIs(languages.get(" RU "), languages.RUSSIAN)

    def test_non_english_presets_use_a_multilingual_model(self):
        for code, preset in languages.PRESETS.items():
            if code == "en":
                continue
            with self.subTest(language=code):
                self.assertFalse(
                    languages.is_english_only(preset.model),
                    f"{preset.model} cannot transcribe {code}",
                )

    def test_short_wake_words_are_backed_by_explicit_forms(self):
        """A wake word too short to widen safely must list its own spellings."""
        minimum = TriggerConfig().fuzzy_min_length
        for code, preset in languages.PRESETS.items():
            for wake in preset.wake_words:
                if len(wake) < minimum:
                    with self.subTest(language=code, wake=wake):
                        self.assertTrue(
                            preset.aliases,
                            f"{wake!r} is too short for fuzzy matching and has no aliases",
                        )

    def test_russian_avoids_the_word_for_code(self):
        """"Клод" is one edit from "код"; the preset must not rely on that."""
        self.assertNotIn("код", languages.RUSSIAN.wake_words + languages.RUSSIAN.aliases)
        for wake in languages.RUSSIAN.wake_words:
            self.assertGreaterEqual(len(wake), TriggerConfig().fuzzy_min_length)


class ApplyLanguageTests(unittest.TestCase):
    def test_russian_sets_everything_a_language_implies(self):
        config = apply_language(Config(), "ru")
        self.assertEqual(config.language, "ru")
        self.assertEqual(config.transcribe.language, "ru")
        self.assertEqual(config.transcribe.model, "small")
        self.assertEqual(config.trigger.wake_words, ["клавдий"])
        self.assertIn("отмена", config.trigger.cancel_phrases)
        self.assertIn("пожалуйста", config.trigger.filler)
        self.assertEqual(config.speech.lang, "ru-RU")
        self.assertIn("Клавдий", config.transcribe.initial_prompt)

    def test_english_is_the_default_and_round_trips(self):
        self.assertEqual(Config(), apply_language(Config(), "en"))

    def test_client_settings_carry_the_language(self):
        settings = apply_language(Config(), "ru").client_settings()
        self.assertEqual(settings["language"], "ru")
        self.assertEqual(settings["speech"]["lang"], "ru-RU")

    def test_an_english_only_model_is_rejected_for_other_languages(self):
        config = apply_language(Config(), "ru")
        config.transcribe.model = "base.en"
        with self.assertRaises(ConfigError) as ctx:
            config.validate()
        self.assertIn("English-only", str(ctx.exception))
        self.assertIn("small", str(ctx.exception))

    def test_english_defaults_pass_validation(self):
        Config().validate()


class ConfigFileTests(unittest.TestCase):
    def write(self, tmp, body):
        path = Path(tmp) / "micclaude.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_language_key_applies_the_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(self.write(tmp, 'language = "ru"\n'))
        self.assertEqual(config.trigger.wake_words, ["клавдий"])
        self.assertEqual(config.transcribe.model, "small")

    def test_explicit_settings_win_over_the_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(
                self.write(
                    tmp,
                    'language = "ru"\n\n[transcribe]\nmodel = "medium"\n\n'
                    '[trigger]\nwake_words = ["ассистент"]\n',
                )
            )
        self.assertEqual(config.language, "ru")
        self.assertEqual(config.transcribe.model, "medium")
        self.assertEqual(config.trigger.wake_words, ["ассистент"])
        self.assertIn("клавдия", config.trigger.aliases, "the rest of the preset stays")

    def test_unknown_language_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'language = "kl"\n')
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
        self.assertIn("no preset for language", str(ctx.exception))

    def test_language_must_be_a_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "language = 42\n")
            with self.assertRaises(ConfigError):
                load_config(path)


class CliTests(unittest.TestCase):
    def parse(self, *argv):
        return apply_overrides(Config(), build_parser().parse_args(list(argv)))

    def test_lang_flag_applies_the_preset(self):
        config = self.parse("--lang", "ru")
        self.assertEqual(config.trigger.wake_words, ["клавдий"])
        self.assertEqual(config.speech.lang, "ru-RU")

    def test_later_flags_win_over_the_preset(self):
        config = self.parse("--lang", "ru", "--model", "medium", "--wake", "ассистент")
        self.assertEqual(config.transcribe.model, "medium")
        self.assertEqual(config.trigger.wake_words, ["ассистент"])

    def test_unknown_language_is_refused_by_the_parser(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--lang", "kl"])

    def test_english_only_model_with_a_foreign_language_exits_two(self):
        import io
        from contextlib import redirect_stderr

        from micclaude.cli import main

        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--lang", "ru", "--model", "base.en", "--print-config"])
        self.assertEqual(code, 2)
        self.assertIn("English-only", err.getvalue())


@unittest.skipUnless(shutil.which("node"), "node is needed to read the browser defaults")
class DefaultsInSyncTests(unittest.TestCase):
    """The browser holds its own copy of these defaults; they must agree."""

    def browser_defaults(self, module: str, name: str) -> dict:
        source = f"import('./web/js/{module}').then(m => console.log(JSON.stringify(m.{name})))"
        output = subprocess.run(
            ["node", "-e", source],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(output.stdout)

    def test_trigger_defaults_match(self):
        from dataclasses import asdict

        self.assertEqual(self.browser_defaults("trigger.js", "DEFAULT_TRIGGER"), asdict(TriggerConfig()))

    def test_audio_defaults_match(self):
        from dataclasses import asdict

        from micclaude.config import AudioConfig

        self.assertEqual(self.browser_defaults("segmenter.js", "DEFAULT_AUDIO"), asdict(AudioConfig()))


if __name__ == "__main__":
    unittest.main()
