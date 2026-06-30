"""Unit tests for the stream-json progress parser (autobuild/progress.py).

The harness spawns `claude -p --output-format stream-json --verbose`, so session.out is
newline-delimited JSON events. The parser must be TOTAL — `autobuild status` and the
supervisor read session.out while it is still being written, so a torn trailing line, a
non-JSON line, or an unexpected shape must be skipped, never raised.
"""

from autobuild.progress import SessionProgress, parse_progress, read_progress

INIT = '{"type":"system","subtype":"init","model":"claude-stub"}'


def _asst(i):
    return ('{"type":"assistant","message":{"model":"claude-x",'
            f'"content":[{{"type":"text","text":"turn {i}"}}]}}}}')


def _result(cost):
    return ('{"type":"result","subtype":"success","num_turns":3,'
            f'"total_cost_usd":{cost},'
            '"usage":{"input_tokens":100,"output_tokens":20}}')


def test_empty_text_is_zero():
    p = parse_progress("")
    assert p == SessionProgress()
    assert p.running is True
    assert p.finished is False


def test_counts_assistant_messages_while_running():
    text = "\n".join([INIT, _asst(0), _asst(1)])  # no result yet
    p = parse_progress(text)
    assert p.messages == 2
    assert p.finished is False
    assert p.cost_usd is None


def test_reads_cost_from_result_event():
    text = "\n".join([INIT, _asst(0), _result(0.1234)])
    p = parse_progress(text)
    assert p.finished is True
    assert abs(p.cost_usd - 0.1234) < 1e-9
    assert p.messages == 1


def test_ignores_torn_trailing_line():
    # a partial last line (file mid-write) has no newline and is invalid JSON
    text = "\n".join([INIT, _asst(0)]) + '\n{"type":"assist'
    p = parse_progress(text)
    assert p.messages == 1  # partial line skipped, not raised


def test_ignores_non_json_and_non_dict_lines():
    text = "\n".join(["not json at all", "[1,2,3]", INIT, _asst(0)])
    p = parse_progress(text)
    assert p.messages == 1


def test_cost_rejects_bool():
    # JSON `true` must not be read as a cost of 1.0
    text = '{"type":"result","subtype":"success","total_cost_usd":true}'
    p = parse_progress(text)
    assert p.finished is True
    assert p.cost_usd is None


def test_cost_rejects_non_number():
    text = '{"type":"result","subtype":"success","total_cost_usd":"lots"}'
    assert parse_progress(text).cost_usd is None


def test_result_without_cost_is_finished_but_costless():
    # an errored/killed result line may omit total_cost_usd
    text = '{"type":"result","subtype":"error_max_turns","is_error":true}'
    p = parse_progress(text)
    assert p.finished is True
    assert p.cost_usd is None


def test_error_result_still_captures_cost():
    # error_max_turns exits non-zero but the result event still carries the real cost
    text = ('{"type":"result","subtype":"error_max_turns","is_error":true,'
            '"total_cost_usd":0.065552}')
    p = parse_progress(text)
    assert p.finished is True
    assert abs(p.cost_usd - 0.065552) < 1e-9


def test_read_progress_missing_file(tmp_path):
    assert read_progress(tmp_path / "nope.out") == SessionProgress()


def test_read_progress_reads_file(tmp_path):
    f = tmp_path / "session.out"
    f.write_text("\n".join([INIT, _asst(0), _result(0.5)]), encoding="utf-8")
    p = read_progress(f)
    assert p.finished and abs(p.cost_usd - 0.5) < 1e-9 and p.messages == 1
