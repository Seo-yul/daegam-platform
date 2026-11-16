import pytest

from daegam.pipeline.convert import MAX_UPLOAD_BYTES, PipelineError, run
from tests.helpers.backend_apis import fixture_bytes


def _assert_complete(result):
    assert result.report["ok"] is True
    assert result.report["complete"] is True
    assert result.report["summary"]["skip"] == 0
    assert result.report["summary"]["fail"] == 0
    assert result.yaml.startswith("openapi:")
    assert result.tool_count == len(result.tool_names)


def test_calculator_wsdl():
    r = run(fixture_bytes("calculator.wsdl"), filename="calculator.wsdl")
    assert r.kind == "soap"
    assert r.source_format == "wsdl"
    assert r.tool_names == ["Add", "Divide"]
    assert [d["operation_id"] for d in r.tool_details] == ["Add", "Divide"]
    add = r.tool_details[0]
    assert "Adds" in (add["summary"] or add["description"] or "")
    _assert_complete(r)


def test_legacy_swagger2():
    r = run(fixture_bytes("legacy-swagger.json"), filename="legacy-swagger.json")
    assert r.kind == "rest"
    assert r.source_format == "swagger2"
    assert r.spec["openapi"].startswith("3.")
    assert r.tool_count == 5
    _assert_complete(r)


def test_openapi3_passthrough():
    r = run(fixture_bytes("orders.openapi.yaml"), filename="orders.openapi.yaml")
    assert r.kind == "soap"
    assert r.source_format == "openapi3"
    assert r.tool_names == ["CreateOrder", "GetOrder"]
    _assert_complete(r)


def test_mixed_rejected():
    with pytest.raises(PipelineError) as ei:
        run(fixture_bytes("mixed.openapi.yaml"), filename="mixed.openapi.yaml")
    assert ei.value.code == "mixed"
    assert ei.value.report is None


def test_too_large():
    with pytest.raises(PipelineError) as ei:
        run(b"x" * (MAX_UPLOAD_BYTES + 1))
    assert ei.value.code == "too_large"


def test_unsupported():
    with pytest.raises(PipelineError) as ei:
        run(b"hello: world\n", filename="x.yaml")
    assert ei.value.code == "unsupported"


def test_conversion_error_on_broken_wsdl():
    # XML은 멀쩡하지만 binding이 없는 portType을 가리키는 WSDL
    broken = fixture_bytes("calculator.wsdl").replace(
        b'type="tns:CalculatorPortType"', b'type="tns:NoSuchPortType"'
    )
    with pytest.raises(PipelineError) as ei:
        run(broken, filename="broken.wsdl")
    assert ei.value.code == "conversion"
    assert ei.value.report is None


def test_verify_failure_carries_report():
    empty = b'{"openapi":"3.0.3","info":{"title":"t","version":"1"},"paths":{}}'
    with pytest.raises(PipelineError) as ei:
        run(empty, filename="empty.json")
    assert ei.value.code == "verify"
    assert ei.value.report["ok"] is False
    assert any(r["status"] == "fail" for r in ei.value.report["results"])
