# 🎬 Auto Sync

팟플레이어 재생 중 화면(입모양)과 오디오를 실시간 분석하여  
싱크를 자동 보정하는 멀티코어 GUI 프로그램입니다.  
**Win32 PostMessage** 방식으로 다른 창에 키 입력 간섭이 전혀 없습니다.

---

## 📥 EXE 다운로드

1. 이 저장소의 **Actions** 탭 클릭
2. 가장 최근 **Build EXE** 워크플로우 클릭
3. 하단 **Artifacts** 섹션에서 `AutoSync-EXE` 다운로드
4. 압축 해제 후 `Auto Sync.exe` 실행

> Windows 10 / 11 지원

---

## 🔐 인증 시스템

처음 실행 시 사용 허가 인증이 필요합니다.

1. 프로그램 실행 → 인증 팝업에서 **사용자명 입력** 후 확인
2. 개발자에게 인증 요청 메일이 자동 발송됨
3. 개발자가 메일의 **[허가]** 링크 클릭
4. 프로그램이 자동으로 허가를 감지하여 바로 실행됨

인증 정보는 로컬에 저장되어 이후 실행 시 자동 검증됩니다.  
인증이 취소된 경우 안내 팝업 후 프로그램이 종료됩니다.

---

## 🖥️ UI 구성

| 영역 | 설명 |
|------|------|
| 상태 카드 | 팟플레이어 연결 / 오디오 장치 / 프로세스 상태 |
| OFFSET 미터 | 현재 싱크 오프셋(ms) + 진행바 |
| 하단 정보 | 이미지 샘플 / 오디오 샘플 / 누적 보정값 |
| 버튼 | ▶ 시작 · ↺ 초기화 · ✕ 종료 |
| ⚙ 메뉴 | 설정 팝업 / 로그 보기 |

### 테마 / 크기

- 다크 모드 / 라이트 모드 전환 지원
- UI 크기 소 / 중 / 대 선택 가능
- 모든 팝업 크기 비례 스케일 적용

---

## ⚙️ 설정

| 항목 | 설명 |
|------|------|
| Windows 시작 시 자동 실행 | 부팅 시 프로그램 자동 실행 (레지스트리 등록) |
| 프로그램 실행 시 자동 시작 | 켜지면 바로 팟플레이어 감지 시작 |
| 다크 모드 | 다크 / 라이트 테마 전환 |
| UI 크기 | 소 / 중 / 대 선택 |

---

## 🔔 알림 기능

- **팟플레이어 감지** → Windows 토스트 알림
- **동영상 재생 감지** → 시작 여부 확인 팝업 (자동 시작 OFF 시)
- **재생 종료 감지** → 자동 중지 후 다음 재생 대기
- **싱크 보정 시작** → 토스트 알림

---

## 🗂️ 트레이 아이콘

창 X 버튼을 누르면 시스템 트레이로 최소화됩니다.  
트레이 아이콘 우클릭 시:

| 항목 | 설명 |
|------|------|
| Auto Sync 열기 | 창 다시 표시 |
| ▶ 싱크 시작 / ⏹ 싱크 중지 | 실행 상태에 따라 토글 |
| 종료 | 프로그램 완전 종료 |

---

## 🏗️ 프로세스 구조

```
[P1] LipCapture   — 화면캡처 + 애니 얼굴 감지  (코어 1)
[P2] AudioCapture — WASAPI 팟플레이어 전용      (코어 2)
[P3] Analyzer     — 싱크 분석 + Win32 보정      (코어 3)
[P4] GUI          — tkinter 상태창              (코어 4)
```

### 최적화 내용

- 팟플레이어 창 영역만 캡처 (전체 화면 X)
- 얼굴 감지는 5프레임마다 1회, 나머지는 마지막 ROI 재사용
- 영상 변경 감지 시 자동 싱크 초기화

---

## ⚙️ 주요 설정값

| 항목 | 기본값 | 설명 |
|------|--------|------|
| SYNC_THRESHOLD_MS | 80ms | 이 이상 어긋나야 보정 실행 |
| POTPLAYER_STEP_MS | 50ms | 키 1회당 보정량 |
| MAX_CORRECT_STEP | 10회 | 1회 분석당 최대 보정 횟수 |
| MAX_TOTAL_SYNC_MS | 500ms | 영상별 누적 보정 상한 |
| ANALYSIS_INTERVAL | 3.0초 | 싱크 분석 주기 |
| BUFFER_SEC | 3.0초 | 분석 버퍼 길이 |
| CAPTURE_FPS | 15fps | 화면 캡처 프레임 |

---

## 📁 파일 구조

```
potplayer_lipsync_mp.py   진입점
win32_utils.py            Win32 제어 / 설정값
processes.py              P1·P2·P3 프로세스
gui_base.py               테마·설정·트레이
gui_ui.py                 창/UI/팝업 구성
gui_run.py                실행 제어·인증 팝업
auth.py                   인증 모듈
auth_server.gs            Google Apps Script 인증 서버
lbpcascade_animeface.xml  애니메이션 얼굴 감지 모델
scripts/make_icon.py      빌드용 아이콘 생성
scripts/make_version.py   빌드용 버전 정보 생성
```

---

## 📦 의존성 (소스 실행 시)

```bash
pip install opencv-python numpy pyaudiowpatch sounddevice scipy \
            mss pywin32 comtypes psutil pystray Pillow winotify
```

---

## 🏷️ 빌드

GitHub Actions로 자동 빌드됩니다.  
`main` 브랜치에 푸시하거나 Actions 탭에서 수동 실행 시 빌드됩니다.

- Python 3.11
- PyArmor 난독화 적용
- PyInstaller 단일 EXE 패키징
- Artifact 보관 기간: 3일

---

## 📝 저작권

Sinamon
