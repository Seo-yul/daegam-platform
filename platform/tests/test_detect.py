from daegam.pipeline import detect as detect_mod
from daegam.pipeline.detect import detect
from daegam.xmlsafe import safe_xml_parser
from tests.helpers.documents import BILLION_LAUGHS


def test_detect_wsdl_namespace_required():
    fmt, _ = detect(b'<definitions xmlns="http://example.com/not-wsdl"/>')
    assert fmt == "unsupported"


def test_detect_garbage():
    assert detect(b"\x00\x01\x02 not a spec")[0] == "unsupported"
    assert detect(b"just: yaml\nwithout: openapi\n")[0] == "unsupported"
    assert detect(b"- a list\n- not a mapping\n")[0] == "unsupported"


def test_wsdl_detection_reads_with_the_safe_parser(tmp_path):
    """판별은 `xmlsafe` 의 안전 파서를 쓴다(D61 에서 옮겼다). 외부 엔티티를 풀지 않고 billion
    laughs 는 구문 오류라 unsupported 다."""
    assert detect_mod.safe_xml_parser is safe_xml_parser
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    xxe = (
        f'<!DOCTYPE d [<!ENTITY e SYSTEM "file://{secret}">]>'
        '<definitions xmlns="http://schemas.xmlsoap.org/wsdl/">&e;</definitions>'
    )
    assert detect(xxe.encode()) == ("wsdl", None)
    root = '<definitions xmlns="http://schemas.xmlsoap.org/wsdl/">&lol9;</definitions>'
    bomb = BILLION_LAUGHS + root
    assert detect(bomb.encode())[0] == "unsupported"
