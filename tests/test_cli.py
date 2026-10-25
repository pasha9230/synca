import json

from tires_sync.cli import main
from helpers import catalog_xml, commodity_xml, settings_dict


def test_dry_run_command_writes_feed_and_returns_zero(tmp_path, capsys):
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml()))
    (tmp_path / "reference.txt").write_text(
        '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps(settings_dict()), encoding="utf-8")
    code = main([
        "dry-run", "--xml", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt"),
        "--settings", str(tmp_path / "settings.json"), "--db", str(tmp_path / "t.db"), "--out", str(tmp_path / "out"),
    ])
    assert code == 0
    assert (tmp_path / "out" / "avito_feed.xml").exists()
    assert "publish" in capsys.readouterr().out


def _files(tmp_path, model="Test Model 2"):
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(SMODEL=model)))
    (tmp_path / "reference.txt").write_text(
        '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps(settings_dict()), encoding="utf-8")
    return ["--db", str(tmp_path / "t.db")]


def test_mapping_flow_makes_the_item_publishable_using_settings_from_db(tmp_path, capsys):
    db = _files(tmp_path)
    xml = ["--xml", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt")]
    assert main(["settings", "init", "--file", str(tmp_path / "settings.json")] + db) == 0
    assert main(["mappings", "pending"] + xml + db) == 0
    out = capsys.readouterr().out
    assert "Test Model 2" in out and "Test Model" in out
    assert main(["mappings", "confirm", "--brand", "Ikon Tyres", "--model", "Test Model 2", "--to", "Test Model",
                 "--reference", str(tmp_path / "reference.txt")] + db) == 0
    assert main(["dry-run"] + xml + db + ["--out", str(tmp_path / "out")]) == 0
    assert '"publish": 1' in capsys.readouterr().out
    assert (tmp_path / "out" / "avito_feed.xml").exists()


def test_commands_without_initialized_settings_report_a_clear_error(tmp_path, capsys):
    db = _files(tmp_path)
    xml = ["--xml", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt")]
    assert main(["mappings", "pending"] + xml + db) == 2
    assert "settings init" in capsys.readouterr().err


def test_confirm_with_unknown_target_model_is_a_user_error(tmp_path, capsys):
    db = _files(tmp_path)
    assert main(["settings", "init", "--file", str(tmp_path / "settings.json")] + db) == 0
    code = main(["mappings", "confirm", "--brand", "Ikon Tyres", "--model", "X", "--to", "Nope",
                 "--reference", str(tmp_path / "reference.txt")] + db)
    assert code == 2
    assert "в справочнике Авито" in capsys.readouterr().err
