from pathlib import Path


APP = Path(__file__).parents[1] / "app.py"
SOURCE = APP.read_text(encoding="utf-8")


def test_skip_command_is_registered():
    assert "async def skip_command" in SOURCE
    assert 'CommandHandler("skip", skip_command)' in SOURCE


def test_skip_only_advances_optional_steps():
    assert 'if step == "details":' in SOURCE
    assert 'if step == "attachment":' in SOURCE
    assert 'state["data"]["additional_details"] = ""' in SOURCE
    assert 'finalize_request(update, context, state, state.get("request_id"))' in SOURCE
