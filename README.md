# ktl-field-server — B-1 현장 서버 (백엔드)

담당 **여준** · 리뷰 짝 희승 · 과제 B(현장 사진·데이터)

노트북에서 돌아가는 FastAPI 서버. 휴대폰 사진·로거 파일을 받아 원본 보관 → 모듈로 판독·병합 → 시험자가 확정한 값을 xlsm에 붙일 형식으로 내보낸다.

| 기준 문서 | |
|---|---|
| 요구사항 | [REQ-B1-01~14](https://github.com/YC-Dr-link/ktl-hub/blob/main/docs/01-요구사항명세.md#4-b-1-현장-서버-백엔드--여준) |
| API (사람용) | [02 §1~2](https://github.com/YC-Dr-link/ktl-hub/blob/main/docs/02-API명세.md) |
| API (기준) | [field-server.openapi.yaml](https://github.com/YC-Dr-link/ktl-hub/blob/main/docs/api/field-server.openapi.yaml) — 서버의 `/docs`가 이것과 같아야 함 |
| 모듈 계약 | [02 §3](https://github.com/YC-Dr-link/ktl-hub/blob/main/docs/02-API명세.md#3-모듈-계약-b-3--b-4--b-1) · [ktl-field-modules/contract](https://github.com/YC-Dr-link/ktl-field-modules/tree/main/contract) |
| 화면 흐름 | [시나리오 B~D](https://github.com/YC-Dr-link/ktl-hub/blob/main/docs/03-UI시나리오.md#시나리오-b--현장-시료채취기-사진-판독부터-내보내기까지) |

## 구성 (제안)

```
app/
  main.py          두 앱: 휴대폰(:8765) · 관리(127.0.0.1:8766)
  api/             라우터 (auth, cases, photos, events, admin)
  models/          SQLAlchemy 2 · Alembic
  services/        보관소(원본 읽기 전용 + SHA-256), 확정·이력, 내보내기, 해시 체인 로그
  worker/          판독 작업 큐 (multiprocessing, 1장 10초 제한)
  modules.py       categories.yaml 기준 Reader/Selector 로더
static/            ktl-field-web 빌드 결과 (mobile/, admin/)
storage/           originals/ derived/ submit/ exports/  (git 제외)
tests/
```

모듈 설치 (개발용):
```
pip install -e "git+https://github.com/YC-Dr-link/ktl-field-modules#egg=ktl-contract&subdirectory=contract"
pip install -e "git+https://github.com/YC-Dr-link/ktl-field-modules#egg=ktl-readers&subdirectory=readers"
pip install -e "git+https://github.com/YC-Dr-link/ktl-field-modules#egg=ktl-selectors&subdirectory=selection"
```
처음에는 `ktl_contract.fakes`의 가짜 모듈로 끝까지 동작시키는 것이 목표(관문 2).

## 라돈 파일·CSV 내보내기 API (모듈 #13 연결 작업)

관리 앱에만 다음 경로를 추가했다. 기본 주소는 `http://127.0.0.1:8766/api/admin/v1`이다.

| 메서드 | 경로 | 동작 |
|---|---|---|
| POST | `/cases/{case_id}/files` | multipart `file` 1개를 보관하고 SHA-256·판별 결과 반환 |
| GET | `/files/{file_id}/series` | 실제 Selector의 시계열 |
| POST | `/cases/{case_id}/merges` | `selector`, `file_ids`, `options`를 받아 202와 작업 ID 반환 |
| GET | `/merges/{merge_id}` | 상태·표·경고·계산 근거 |
| POST | `/cases/{case_id}/exports` | 완료된 병합을 CSV로 변환하는 작업 접수 |
| GET | `/exports/{export_id}` | 상태와 앞 20행 미리보기 |
| GET | `/exports/{export_id}/download` | 준비된 CSV 첨부파일 다운로드 |

내보내기 요청 예: `{"kind":"merge_raw","format":"csv","encoding":"utf-8","merge_id":"mg_..."}`.
UTF-8은 Excel에서 확인하기 쉽도록 BOM을 넣고, cp949도 지원한다. `txt/xlsx`, 사진 ZIP과
`photo_values`는 이 작업에 포함하지 않아 요청 시 400으로 알린다.

원본과 작업 상태는 SQLite에 보존한다. 원본은 읽기 전용이며 실행 전에 SHA-256을 확인한다.
Selector는 별도 프로세스에서 최대 10초 실행하고, 실패한 작업만 failed로 기록한다.
기준기 `options.reference`의 파일 ID는 선택한 경로로 바꿔 모듈에 전달한다.
실제 `ktl_selectors` 패키지가 필요하며, 라돈 요청에 가짜 결과를 대신 반환하지 않는다.

서버는 성공한 SelectResult의 표를 그대로 내보낸다. 현재 사용자가 우선 확인하기로 한
`radon_detail_v1`은 모델·시간·항목·값 등의 8열이며 파일마다 측정값 12행 뒤에 평균 1행을 붙인다.
이는 RAW1~4 배치와 다르므로 모듈이 `DETAIL_LAYOUT` 경고와 `meta.pending`을 반환한다.
가짜 Selector로 일반 경로·실패·재시작 동작을, 실제 모듈로 업로드→run→CSV 다운로드를 검증한다.
원래 #13의 RAW1~4·지시오차·반복성 완료 기준은 별도로 남아 있다.

## 실행

```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8765
```
`categories.yaml`은 위 모듈 설치 위치(`src/ktl-contract/`)에서 읽는다. 다른 곳이면 `KTL_CATEGORIES_PATH`로 지정.
