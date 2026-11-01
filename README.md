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

## 실행

```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8765
```
`categories.yaml`은 위 모듈 설치 위치(`src/ktl-contract/`)에서 읽는다. 다른 곳이면 `KTL_CATEGORIES_PATH`로 지정.
