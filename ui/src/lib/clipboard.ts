/**
 * 클립보드에 쓴다. 성공하면 true.
 *
 * **`navigator.clipboard` 만 쓰면 안 된다.** 브라우저는 그것을 보안 컨텍스트(https 또는 localhost)에만
 * 준다. 평문 http 로 여는 배포(로컬의 `http://daegam.test`, `http://daegam.<ip>.sslip.io` 같은 시험 주소)에서는
 * `navigator.clipboard` 가 아예 undefined 다(`isSecureContext: false, hasClipboard: false`).
 *
 * 그래서 폐기 예정인 `document.execCommand("copy")` 를 폴백으로 남긴다. 그것은 보안 컨텍스트를 따지지
 * 않는다. 화면 밖 textarea 를 잠깐 만들어 고르고 복사한 뒤 지운다.
 *
 * 둘 다 안 되면 false 다. 호출부는 그때 조용히 넘어가지 말고 사람에게 말해야 한다. 눌렀는데 아무 일도
 * 안 일어나는 것이 지금까지의 모습이었다.
 *
 * 폴백은 textarea 를 골라야 해서 초점을 가져간다. 그것을 돌려놓지 않으면 키보드로 쓰던 사람이 복사
 * 한 번에 문서 맨 앞으로 돌아간다.
 */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 권한을 막아 둔 경우가 있다. 아래 폴백으로 간다
    }
  }
  const active = document.activeElement;
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  // 화면 밖에 두되 display:none 은 안 된다. 그러면 고를 수 없다
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  try {
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    ta.remove();
    if (active instanceof HTMLElement) active.focus();
  }
}
