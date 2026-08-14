#requires -version 5
# 이 파일은 UTF-8(BOM 있음) + CRLF 로 저장할 것 (.gitattributes / 프로젝트 관례).
#
# 지금 이 콘솔 창의 QuickEdit(빠른 편집) 모드를 끈다.
#
# 왜 필요한가 — mypc_start.bat 은 서버를 "이 창에서 직접" 돌린다(에러를 눈앞에서 보려고).
# Windows 콘솔은 QuickEdit 이 기본 켜짐이라, 창을 클릭하거나 드래그하면 선택 모드로
# 들어가면서 그 프로세스의 stdout 쓰기가 블록된다. 그러면 서버는 살아 있는데 로그를
# 찍으려는 스레드부터 줄줄이 멈추고, 결국 요청을 하나도 처리하지 못한다. 겉으로는
#   * 창은 떠 있고 출력만 멈춰 있음
#   * 클라이언트는 "업로드 100%" 에서 read timeout
#   * 브라우저는 127.0.0.1 네트워크 에러
# 로 보여 서버 코드 문제로 오진하기 쉽다. (선택 모드는 창에서 Enter/Esc 를 누르면 풀린다.)
#
# 적용 범위: 이 스크립트를 부른 콘솔 창 하나뿐이다. 콘솔 입력 버퍼의 모드를 바꾸는
# 것이라 부모 cmd 와 그 뒤에 실행되는 python 에도 그대로 이어지고, 창을 닫으면 사라진다.
# 레지스트리(HKCU\Console)는 건드리지 않으므로 다른 창·다른 프로그램에는 영향이 없다.
#
# 실패해도 서버 기동을 막지 않는다 — 편의 장치일 뿐이다(예: 콘솔이 없는 환경).

$ErrorActionPreference = 'Stop'

try {
    if (-not ('Win32ConsoleMode' -as [type])) {
        Add-Type -Namespace '' -Name 'Win32ConsoleMode' -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern IntPtr GetStdHandle(int nStdHandle);
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);
'@
    }

    $STD_INPUT_HANDLE        = -10
    $ENABLE_QUICK_EDIT_MODE  = 0x0040
    $ENABLE_EXTENDED_FLAGS   = 0x0080

    $handle = [Win32ConsoleMode]::GetStdHandle($STD_INPUT_HANDLE)
    if ($handle -eq [IntPtr]::Zero -or $handle -eq [IntPtr](-1)) {
        Write-Output '[mypc] QuickEdit 해제 건너뜀 (콘솔 입력 핸들 없음)'
        exit 0
    }

    $mode = 0
    if (-not [Win32ConsoleMode]::GetConsoleMode($handle, [ref]$mode)) {
        Write-Output '[mypc] QuickEdit 해제 건너뜀 (콘솔 모드를 읽지 못함)'
        exit 0
    }

    if (($mode -band $ENABLE_QUICK_EDIT_MODE) -eq 0) {
        Write-Output '[mypc] QuickEdit 이미 꺼져 있음 - 창을 클릭해도 서버가 멈추지 않습니다'
        exit 0
    }

    # QuickEdit 비트만 내린다. SetConsoleMode 로 QuickEdit 을 끄려면 EXTENDED_FLAGS 가
    # 함께 켜져 있어야 한다(Windows 규약) — 없으면 조용히 무시된다.
    $newMode = ($mode -band (-bnot $ENABLE_QUICK_EDIT_MODE)) -bor $ENABLE_EXTENDED_FLAGS
    if ([Win32ConsoleMode]::SetConsoleMode($handle, $newMode)) {
        Write-Output '[mypc] QuickEdit 해제 완료 - 이 창을 클릭해도 서버가 멈추지 않습니다'
    } else {
        Write-Output '[mypc] WARN: QuickEdit 을 끄지 못했습니다 - 이 창을 클릭/드래그하지 마세요'
    }
} catch {
    Write-Output ('[mypc] WARN: QuickEdit 해제 실패 ({0}) - 이 창을 클릭/드래그하지 마세요' -f $_.Exception.Message)
}

exit 0
