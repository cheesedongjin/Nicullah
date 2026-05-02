# AI Agent War Room

Gemini 기반의 가상 프로그래밍 팀이 실제 프로젝트 폴더 안에서 파일을 읽고, 쓰고, 수정하고, 테스트를 실행하는 로컬 워룸 앱입니다.

## 핵심 동작

- 프로젝트마다 `projects/<project-slug>/` 폴더가 생성됩니다.
- 각 프로젝트 폴더에는 `project_context.md`, `README.md`, `.warroom/state.json`이 만들어집니다.
- PM, Designer, Backend, Frontend, QA 에이전트가 파이프라인에 따라 반복 작업합니다.
- PO 승인이 필요한 에이전트만 blocked 상태가 되고, 다른 에이전트는 계속 진행할 수 있습니다.
- 에이전트는 티켓, 핸드오프, 리뷰, 명령 실행 결과, UI 프리뷰 분석 결과를 공유 상태로 주고받습니다.
- 선택형 의사결정은 팝업에서만 표시됩니다.
- Gemini가 없으면 로컬 폴백 없이 프로젝트 생성과 실행이 막힙니다.

## 역할별 기능

- PM: `docs/PRD.md`, `docs/backlog.md`, 티켓 분할, 우선순위, 의존성, 승인 기준을 관리합니다.
- Product Designer: `docs/design_brief.md`, `docs/wireframes.md`, 코드 기반 와이어프레임, 스크린샷 기반 시각 리뷰를 담당합니다.
- Backend Engineer: `docs/api.md` 또는 `openapi.yaml`, `docs/database.md`, API, DB 스키마, 비즈니스 로직, 서버 구조를 구현합니다.
- Frontend Engineer: React/Next.js/Vite/Tailwind 또는 적합한 스택으로 UI를 구현하고, API 연동과 프리뷰 URL을 제공합니다.
- QA / SET: `docs/test_plan.md`, 테스트 시나리오, 자동화 테스트/스모크 스크립트 작성 및 실행, UI 스크린샷 멀티모달 분석, 결함 티켓 라우팅을 담당합니다.

## 팀 상호작용

에이전트는 단순히 순서대로 호출되는 것이 아니라 다음 공유 오브젝트로 서로 작업을 넘깁니다.

- `ticket`: 작업 단위, 담당자, 상태, 우선순위, 승인 기준.
- `handoff`: 특정 역할에게 넘기는 다음 행동, 관련 파일, 티켓, 우선순위.
- `review`: `pass`, `needs_changes`, `blocked` verdict와 발견 사항.
- `command_runs`: 실제 테스트/빌드/스모크 명령 실행 결과.
- `previews`: 웹 미리보기 스크린샷 경로와 Gemini Vision 리뷰 결과.
- `approval_history`: PO 선택형 의사결정 기록.

## 실행

```powershell
python -m pip install -r server/requirements.txt
$env:GEMINI_API_KEY="YOUR_KEY"
python server/app.py
```

앱은 기본적으로 [http://127.0.0.1:4173](http://127.0.0.1:4173)에서 실행됩니다.

## 에이전트가 사용할 수 있는 작업

- `write_file`: 프로젝트 폴더 내부 파일 생성/덮어쓰기
- `append_file`: 파일에 내용 추가
- `edit_file`: 특정 텍스트 치환
- `read_file`: 파일 읽기
- `run_command`: 프로젝트 폴더에서 테스트/빌드 명령 실행
- `capture_preview`: 웹 미리보기 스크린샷 캡처 후 Gemini Vision 리뷰
- `add_ticket`: 작업 티켓 기록
- `update_ticket`: 티켓 상태, 담당자, 설명, 승인 기준, 메모 갱신
- `handoff`: 다른 역할에게 작업, 리뷰, 수정 요청 전달
- `resolve_handoff`: 받은 핸드오프 해결 처리
- `record_review`: 특정 역할의 산출물에 대한 리뷰 결과 기록
- `set_status`, `complete_task`: 프로젝트 상태 갱신

이미지 리뷰는 Gemini 파일 업로드 후 다음 형태로 호출됩니다.

```python
response = client.models.generate_content(
    model="...",
    contents=[my_file, "Caption this image."],
)
```

## 주의

명령 실행은 프로젝트 폴더 내부에서만 수행되며, `.warroom` 내부 상태 파일은 에이전트가 직접 편집할 수 없습니다.
