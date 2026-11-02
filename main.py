"""
REST-интерфейс «Редактор диаграмм последовательности» (FastAPI).

Модель — диаграмма классов из отчёта:
    Project 1 ── * Diagram
    Diagram 1 ── * Participant   (type: actor..database, position)
    Diagram 1 ── * Fragment      (type: alt..critical, parent_id → вложенность)
    Diagram 1 ── * Message       (source_id/target_id → Participant, fragment_id → Fragment)

Маршруты
  Сервис       GET    /health
  Проекты      GET    /projects                          ?limit=&offset=
               POST   /projects
               GET    /projects/{project_id}
               PUT    /projects/{project_id}
               PATCH  /projects/{project_id}
               DELETE /projects/{project_id}             каскадно с диаграммами
  Диаграммы    GET    /projects/{project_id}/diagrams    ?limit=&offset=
               POST   /projects/{project_id}/diagrams
               GET    /diagrams/{diagram_id}             заголовок ETag = версия
               PUT    /diagrams/{diagram_id}             If-Match → 409 при чужой правке
               PATCH  /diagrams/{diagram_id}
               DELETE /diagrams/{diagram_id}
  Участники    POST   /diagrams/{diagram_id}/participants              FR2
               PATCH  /diagrams/{diagram_id}/participants/{item_id}    FR5
               DELETE /diagrams/{diagram_id}/participants/{item_id}    FR6, каскад сообщений
  Сообщения    POST   /diagrams/{diagram_id}/messages                  FR3
               PATCH  /diagrams/{diagram_id}/messages/{item_id}
               DELETE /diagrams/{diagram_id}/messages/{item_id}
  Фрагменты    POST   /diagrams/{diagram_id}/fragments                 FR4
               PATCH  /diagrams/{diagram_id}/fragments/{item_id}
               DELETE /diagrams/{diagram_id}/fragments/{item_id}       содержимое поднимается выше
  Операции     POST   /diagrams/{diagram_id}/validate                  FR11
               GET    /diagrams/{diagram_id}/export?format=puml|svg|png  FR7, NF3
               POST   /projects/{project_id}/import                    FR8 (PlantUML)
               GET    /diagrams/{diagram_id}/file                      FR9 (файл проекта)
               POST   /projects/{project_id}/diagrams/upload           FR10

Ошибки (единый формат, описан в Swagger)
  404 / 409 / 501 → {"error": "not_found" | "conflict" | ..., "detail": "..."}
  422             → {"error": "validation_error", "details": [{"field", "message"}]}

Переменные окружения
  PORT=9000  STORAGE_DIR=storage  AUTOSAVE_SEC=30 (NF4)  SEED_DEMO=1  CORS_ORIGINS=*

Запуск:   pip install fastapi uvicorn
          python main.py            → http://127.0.0.1:9000/docs
"""
import asyncio
import copy
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from html import escape
from http import HTTPStatus
from pathlib import Path as FsPath
from typing import Annotated, Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
# 404 на несуществующий путь и 405 бросает Starlette — ловим базовый класс
from starlette.exceptions import HTTPException as StarletteHTTPException

# =============================================================================
# Настройки
# =============================================================================
PORT = int(os.getenv("PORT", "9000"))
STORAGE_DIR = FsPath(os.getenv("STORAGE_DIR", "storage"))
STATE_FILE = STORAGE_DIR / "state.json"
AUTOSAVE_SEC = int(os.getenv("AUTOSAVE_SEC", "30"))            # NF4
SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"                 # демо-данные при пустом хранилище
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
MAX_ITEMS = 500                                                # предел элементов в списке


# =============================================================================
# Перечисления — допустимые значения type из диаграммы классов
# =============================================================================
class ParticipantType(str, Enum):
    actor = "actor"
    participant = "participant"
    boundary = "boundary"
    control = "control"
    entity = "entity"
    database = "database"


class MessageType(str, Enum):
    sync = "sync"
    async_ = "async"
    reply = "reply"
    create = "create"
    destroy = "destroy"


class FragmentType(str, Enum):
    alt = "alt"
    opt = "opt"
    loop = "loop"
    par = "par"
    critical = "critical"


# =============================================================================
# Типы полей с ограничениями (переиспользуются в In/Patch-моделях)
# Кавычки в именах и переводы строк везде запрещены: они ломают PlantUML-код.
# =============================================================================
NAME_RE = r'^[^"\r\n]*$'
LINE_RE = r"^[^\r\n]*$"

Title = Annotated[str, Field(min_length=1, max_length=100, pattern=NAME_RE)]
Name = Annotated[str, Field(min_length=1, max_length=60, pattern=NAME_RE)]
Text = Annotated[str, Field(max_length=200, pattern=LINE_RE)]
Description = Annotated[str, Field(max_length=500)]
Id = Annotated[int, Field(ge=1)]
Order = Annotated[int, Field(ge=0)]


class StrictModel(BaseModel):
    """extra="forbid": неизвестное поле в запросе → 422; пробелы по краям срезаются."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# =============================================================================
# Модели: Project
# =============================================================================
class ProjectIn(StrictModel):
    name: Title
    description: Description = ""


class ProjectPatch(StrictModel):
    name: Optional[Title] = None
    description: Optional[Description] = None


class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    diagrams_count: int


# =============================================================================
# Модели: элементы диаграммы
#   *Create — тело POST (id выдаёт сервер)
#   без суффикса — элемент внутри диаграммы (id задан, локальный в диаграмме)
#   *Patch  — тело PATCH (все поля необязательны)
# =============================================================================
class ParticipantCreate(StrictModel):
    name: Name
    type: ParticipantType = ParticipantType.participant
    position: Optional[Order] = Field(None, description="Порядок на оси; не задан — в конец")


class Participant(ParticipantCreate):
    id: Id


class ParticipantPatch(StrictModel):
    name: Optional[Name] = None
    type: Optional[ParticipantType] = None
    position: Optional[Order] = None


class FragmentBase(StrictModel):
    type: FragmentType
    condition: Text = Field("", description="guard-условие")
    parent_id: Optional[Id] = Field(None, description="Родительский фрагмент (FR4)")
    seq_order: Order = 0


class Fragment(FragmentBase):
    id: Id


class FragmentCreate(FragmentBase):
    message_ids: list[Id] = Field(default_factory=list, max_length=MAX_ITEMS,
                                  description="Сообщения, которые обернуть фрагментом")


class FragmentPatch(StrictModel):
    type: Optional[FragmentType] = None
    condition: Optional[Text] = None
    parent_id: Optional[Id] = None
    seq_order: Optional[Order] = None


class MessageCreate(StrictModel):
    source_id: Id
    target_id: Id
    type: MessageType = MessageType.sync
    label: Text = ""
    fragment_id: Optional[Id] = Field(None, description="null — верхний уровень")
    seq_order: Optional[Order] = Field(None, description="Не задан — в конец; занят — остальные сдвигаются")


class Message(MessageCreate):
    id: Id


class MessagePatch(StrictModel):
    source_id: Optional[Id] = None
    target_id: Optional[Id] = None
    type: Optional[MessageType] = None
    label: Optional[Text] = None
    fragment_id: Optional[Id] = None
    seq_order: Optional[Order] = None


# =============================================================================
# Модели: Diagram + проверки целостности
# =============================================================================
def ancestors(fragment_id: Optional[int], parents: dict[int, Optional[int]]) -> list[int]:
    """Цепочка фрагментов от внешнего к внутреннему (циклы отсекаются)."""
    chain: list[int] = []
    while fragment_id is not None and fragment_id not in chain:
        chain.append(fragment_id)
        fragment_id = parents.get(fragment_id)
    return chain[::-1]


def _fill_missing(items: list, attr: str) -> None:
    """Незаданный порядок (None) → в конец, по порядку следования в списке."""
    nxt = max((getattr(i, attr) for i in items if getattr(i, attr) is not None), default=-1) + 1
    for i in items:
        if getattr(i, attr) is None:
            setattr(i, attr, nxt)
            nxt += 1


def _require_unique(values: list, what: str) -> None:
    seen = set()
    for v in values:
        if v in seen:
            raise ValueError(f"{what}: значение {v} повторяется")
        seen.add(v)


class DiagramIn(StrictModel):
    name: Title
    participants: list[Participant] = Field(default_factory=list, max_length=MAX_ITEMS)
    fragments: list[Fragment] = Field(default_factory=list, max_length=MAX_ITEMS)
    messages: list[Message] = Field(default_factory=list, max_length=MAX_ITEMS)

    @model_validator(mode="after")
    def check_integrity(self) -> "DiagramIn":
        """Любое нарушение → ValueError → ответ 422."""
        _fill_missing(self.participants, "position")
        _fill_missing(self.messages, "seq_order")

        # уникальность ключей и порядков (иначе порядок экспорта случаен)
        _require_unique([p.id for p in self.participants], "participants.id")
        _require_unique([p.position for p in self.participants], "participants.position")
        _require_unique([f.id for f in self.fragments], "fragments.id")
        _require_unique([m.id for m in self.messages], "messages.id")
        _require_unique([m.seq_order for m in self.messages], "messages.seq_order")

        # ссылки сообщений (FR3)
        p_ids = {p.id for p in self.participants}
        parents = {f.id: f.parent_id for f in self.fragments}
        for m in self.messages:
            for side in ("source_id", "target_id"):
                if getattr(m, side) not in p_ids:
                    raise ValueError(f"message {m.id}: {side}={getattr(m, side)} — участник не найден")
            if m.fragment_id is not None and m.fragment_id not in parents:
                raise ValueError(f"message {m.id}: fragment_id={m.fragment_id} — фрагмент не найден")

        # родители фрагментов и отсутствие циклов вложенности (FR4)
        for f in self.fragments:
            if f.parent_id is not None and f.parent_id not in parents:
                raise ValueError(f"fragment {f.id}: родитель {f.parent_id} не найден")
            seen, cur = set(), f.id
            while cur is not None:
                if cur in seen:
                    raise ValueError(f"fragment {f.id}: циклическая вложенность")
                seen.add(cur)
                cur = parents[cur]

        # сообщения фрагмента (вместе с вложенными) должны идти подряд,
        # иначе фрагмент «разорвётся» на два блока при экспорте
        order = sorted(self.messages, key=lambda m: m.seq_order)
        chains = [ancestors(m.fragment_id, parents) for m in order]
        for f in self.fragments:
            idx = [i for i, ch in enumerate(chains) if f.id in ch]
            if idx and idx[-1] - idx[0] + 1 != len(idx):
                raise ValueError(f"fragment {f.id}: его сообщения должны идти подряд по seq_order")
        return self


class DiagramPatch(StrictModel):
    name: Optional[Title] = None
    participants: Optional[list[Participant]] = Field(None, max_length=MAX_ITEMS)
    fragments: Optional[list[Fragment]] = Field(None, max_length=MAX_ITEMS)
    messages: Optional[list[Message]] = Field(None, max_length=MAX_ITEMS)


class DiagramOut(DiagramIn):
    id: int
    project_id: int
    version: int
    updated_at: datetime


class DiagramShort(BaseModel):
    id: int
    name: str
    version: int
    updated_at: datetime
    participants: int
    messages: int
    fragments: int


class DiagramFile(StrictModel):
    """Формат файла проекта (FR9/FR10)."""
    format: Literal["seq-editor"] = "seq-editor"
    format_version: Literal[1] = 1
    diagram: DiagramIn


class ImportIn(StrictModel):
    plantuml: str = Field(..., min_length=1, max_length=200_000)
    name: Optional[Title] = Field(None, description="Не задано — берётся из title или по умолчанию")


class ImportOut(BaseModel):
    diagram: DiagramOut
    warnings: list[str]


class ValidationReport(BaseModel):
    valid: bool
    errors: list[str]


# ---------- Модели ошибок — чтобы Swagger показывал реальный формат ----------
class ErrorOut(BaseModel):
    error: str
    detail: str


class FieldError(BaseModel):
    field: str
    message: str


class ValidationErrorOut(BaseModel):
    error: Literal["validation_error"]
    details: list[FieldError]


_ERR_DOCS = {
    404: {"model": ErrorOut, "description": "Объект не найден"},
    409: {"model": ErrorOut, "description": "Версия в If-Match устарела"},
    422: {"model": ValidationErrorOut, "description": "Ошибка валидации"},
    501: {"model": ErrorOut, "description": "Формат не поддерживается"},
}


def errs(*codes: int) -> dict:
    return {c: _ERR_DOCS[c] for c in codes}


# =============================================================================
# Хранилище (в памяти + снимок на диск) и общие помощники
# =============================================================================
LOCK = threading.RLock()        # маршруты синхронные → идут в пуле потоков
projects: dict[int, dict] = {}
diagrams: dict[int, dict] = {}
_next_id = {"project": 0, "diagram": 0}
_state = {"dirty": False}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(kind: str) -> int:
    with LOCK:
        _next_id[kind] += 1
        return _next_id[kind]


def _local_id(items: list[dict]) -> int:
    return max((i["id"] for i in items), default=0) + 1


def _mark_dirty() -> None:
    _state["dirty"] = True


def _fail(loc: tuple, msg: str):
    """Ошибка 422 в том же формате, что и у Pydantic."""
    raise RequestValidationError([{"loc": ("body", *loc), "msg": msg, "type": "value_error"}])


def _validate(model: type[BaseModel], data: dict) -> BaseModel:
    """Проверка собранных на сервере данных; ошибки → 422."""
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise RequestValidationError(e.errors(include_url=False, include_context=False)) from None


def get_project_or_404(project_id: int) -> dict:
    if project_id not in projects:
        raise HTTPException(404, f"Проект #{project_id} не найден")
    return projects[project_id]


def get_diagram_or_404(diagram_id: int) -> dict:
    if diagram_id not in diagrams:
        raise HTTPException(404, f"Диаграмма #{diagram_id} не найдена")
    return diagrams[diagram_id]


def find_or_404(items: list[dict], item_id: int, what: str) -> dict:
    for i in items:
        if i["id"] == item_id:
            return i
    raise HTTPException(404, f"{what} #{item_id} отсутствует в диаграмме")


def check_version(d: dict, if_match: Optional[str]) -> None:
    """Оптимистическая блокировка: If-Match должен совпадать с текущей версией."""
    if if_match is None:
        return
    v = if_match.strip().removeprefix("W/").strip('"')
    if v != str(d["version"]):
        raise HTTPException(409, f"Диаграмма уже изменена: текущая версия {d['version']}, "
                                 f"в If-Match — {v}. Перечитайте её и повторите.")


def set_etag(response: Response, d: dict) -> None:
    response.headers["ETag"] = f'"{d["version"]}"'


def content_of(d: dict) -> dict:
    """Глубокая копия редактируемой части диаграммы."""
    return copy.deepcopy({k: d[k] for k in ("name", "participants", "fragments", "messages")})


def commit(d: dict, content: dict, response: Optional[Response] = None) -> dict:
    """Проверить новое содержимое целиком и только потом применить (иначе 422)."""
    clean = _validate(DiagramIn, content).model_dump(mode="json")
    d.update(clean, version=d["version"] + 1, updated_at=_now())
    _mark_dirty()
    if response is not None:
        set_etag(response, d)
    return d


def create_diagram_record(project_id: int, content: dict) -> dict:
    clean = _validate(DiagramIn, content).model_dump(mode="json")
    with LOCK:
        get_project_or_404(project_id)
        d = {"id": _new_id("diagram"), "project_id": project_id, "version": 1,
             "updated_at": _now(), **clean}
        diagrams[d["id"]] = d
        _mark_dirty()
        return d


def project_out(p: dict) -> dict:
    count = sum(1 for d in diagrams.values() if d["project_id"] == p["id"])
    return {**p, "diagrams_count": count}


def make_room(messages: list[dict], seq: int, exclude_id: Optional[int] = None) -> None:
    """Вставка на занятое место seq_order: сообщения начиная с seq сдвигаются на 1."""
    if any(m["seq_order"] == seq and m["id"] != exclude_id for m in messages):
        for m in messages:
            if m["seq_order"] >= seq and m["id"] != exclude_id:
                m["seq_order"] += 1


# =============================================================================
# Автосохранение (NF4): снимок всего состояния в storage/state.json
# =============================================================================
def save_state() -> None:
    with LOCK:
        if not _state["dirty"]:
            return
        data = json.dumps({"projects": list(projects.values()),
                           "diagrams": list(diagrams.values()),
                           "next_id": _next_id},
                          ensure_ascii=False, indent=1,
                          default=lambda o: o.isoformat())
        _state["dirty"] = False
    try:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, STATE_FILE)                   # атомарная замена файла
    except OSError as e:
        _state["dirty"] = True
        print(f"[autosave] не удалось сохранить: {e}")


def load_state() -> None:
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        with LOCK:
            projects.update({p["id"]: p for p in data["projects"]})
            for d in data["diagrams"]:
                d["updated_at"] = datetime.fromisoformat(d["updated_at"])
                diagrams[d["id"]] = d
            _next_id.update(data["next_id"])
    except (OSError, ValueError, KeyError) as e:
        print(f"[autosave] {STATE_FILE} не прочитан, старт с пустого хранилища: {e}")


async def autosave_loop() -> None:
    while True:
        await asyncio.sleep(AUTOSAVE_SEC)
        await asyncio.to_thread(save_state)


# =============================================================================
# Приложение
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    if SEED_DEMO and not projects:
        seed_demo()
    task = asyncio.create_task(autosave_loop())
    yield
    task.cancel()
    save_state()                                     # финальное сохранение при остановке


app = FastAPI(title="Sequence Diagram Editor API", version="2.0.0", lifespan=lifespan,
              description="REST-интерфейс редактора диаграмм последовательности.")

# CORS — чтобы веб-интерфейс редактора (NF5) мог обращаться к API с другого порта
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["ETag", "Content-Disposition"])


# =============================================================================
# Обработчики ошибок
# =============================================================================
@app.exception_handler(StarletteHTTPException)          # покрывает и fastapi.HTTPException
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    try:   # 404 → not_found, 409 → conflict, 405 → method_not_allowed …
        code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        code = "http_error"
    return JSONResponse(status_code=exc.status_code,
                        content={"error": code, "detail": str(exc.detail)},
                        headers=getattr(exc, "headers", None))   # не терять Allow и др.


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = []
    for e in exc.errors():
        # ("body", "messages", 0, "label") → "messages.0.label"
        field = ".".join(str(x) for x in e["loc"] if x not in ("body", "query", "path", "header"))
        details.append({"field": field or "body",
                        "message": str(e["msg"]).removeprefix("Value error, ")})
    return JSONResponse(status_code=422,
                        content={"error": "validation_error", "details": details})


# Параметры пути, запроса и заголовков — тоже с валидацией (иначе /projects/-5 даст 404, а не 422)
ProjectId = Annotated[int, Path(ge=1)]
DiagramId = Annotated[int, Path(ge=1)]
ItemId = Annotated[int, Path(ge=1)]
Limit = Annotated[int, Query(ge=1, le=100, description="Размер страницы")]
Offset = Annotated[int, Query(ge=0, description="Сдвиг")]
IfMatch = Annotated[Optional[str], Header(alias="If-Match",
                                          description="Ожидаемая версия диаграммы (из ETag)")]


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/health", tags=["Service"])
def health():
    return {"status": "ok", "projects": len(projects), "diagrams": len(diagrams)}


# =============================================================================
# Проекты
# =============================================================================
@app.get("/projects", response_model=list[ProjectOut], tags=["Projects"], responses=errs(422))
def list_projects(limit: Limit = 50, offset: Offset = 0):
    with LOCK:
        return [project_out(p) for p in list(projects.values())[offset:offset + limit]]


@app.post("/projects", response_model=ProjectOut, status_code=201, tags=["Projects"],
          responses=errs(422))
def create_project(body: ProjectIn):
    with LOCK:
        p = {"id": _new_id("project"), **body.model_dump()}
        projects[p["id"]] = p
        _mark_dirty()
        return project_out(p)


@app.get("/projects/{project_id}", response_model=ProjectOut, tags=["Projects"],
         responses=errs(404, 422))
def get_project(project_id: ProjectId):
    with LOCK:
        return project_out(get_project_or_404(project_id))


@app.put("/projects/{project_id}", response_model=ProjectOut, tags=["Projects"],
         responses=errs(404, 422))
def replace_project(project_id: ProjectId, body: ProjectIn):
    with LOCK:
        p = get_project_or_404(project_id)
        p.update(body.model_dump())
        _mark_dirty()
        return project_out(p)


@app.patch("/projects/{project_id}", response_model=ProjectOut, tags=["Projects"],
           responses=errs(404, 422))
def patch_project(project_id: ProjectId, body: ProjectPatch):
    with LOCK:
        p = get_project_or_404(project_id)
        merged = {"name": p["name"], "description": p["description"],
                  **body.model_dump(exclude_unset=True)}
        p.update(_validate(ProjectIn, merged).model_dump())   # явный null → 422
        _mark_dirty()
        return project_out(p)


@app.delete("/projects/{project_id}", status_code=204, tags=["Projects"],
            responses=errs(404, 422))
def delete_project(project_id: ProjectId):
    with LOCK:
        get_project_or_404(project_id)
        for did in [d["id"] for d in diagrams.values() if d["project_id"] == project_id]:
            del diagrams[did]
        del projects[project_id]
        _mark_dirty()


# =============================================================================
# Диаграммы
# =============================================================================
@app.get("/projects/{project_id}/diagrams", response_model=list[DiagramShort],
         tags=["Diagrams"], responses=errs(404, 422))
def list_diagrams(project_id: ProjectId, limit: Limit = 50, offset: Offset = 0):
    with LOCK:
        get_project_or_404(project_id)
        own = [d for d in diagrams.values() if d["project_id"] == project_id]
        return [{"id": d["id"], "name": d["name"], "version": d["version"],
                 "updated_at": d["updated_at"], "participants": len(d["participants"]),
                 "messages": len(d["messages"]), "fragments": len(d["fragments"])}
                for d in own[offset:offset + limit]]


@app.post("/projects/{project_id}/diagrams", response_model=DiagramOut, status_code=201,
          tags=["Diagrams"], responses=errs(404, 422))
def create_diagram(project_id: ProjectId, body: DiagramIn, response: Response):
    d = create_diagram_record(project_id, body.model_dump(mode="json"))
    set_etag(response, d)
    return d


@app.get("/diagrams/{diagram_id}", response_model=DiagramOut, tags=["Diagrams"],
         responses=errs(404, 422))
def get_diagram(diagram_id: DiagramId, response: Response):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        set_etag(response, d)
        return d


@app.put("/diagrams/{diagram_id}", response_model=DiagramOut, tags=["Diagrams"],
         responses=errs(404, 409, 422))
def replace_diagram(diagram_id: DiagramId, body: DiagramIn, response: Response,
                    if_match: IfMatch = None):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        return commit(d, body.model_dump(mode="json"), response)


@app.patch("/diagrams/{diagram_id}", response_model=DiagramOut, tags=["Diagrams"],
           responses=errs(404, 409, 422))
def patch_diagram(diagram_id: DiagramId, body: DiagramPatch, response: Response,
                  if_match: IfMatch = None):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        content.update(body.model_dump(mode="json", exclude_unset=True))
        return commit(d, content, response)


@app.delete("/diagrams/{diagram_id}", status_code=204, tags=["Diagrams"],
            responses=errs(404, 409, 422))
def delete_diagram(diagram_id: DiagramId, if_match: IfMatch = None):
    with LOCK:
        check_version(get_diagram_or_404(diagram_id), if_match)
        del diagrams[diagram_id]
        _mark_dirty()


# =============================================================================
# Участники (FR2, FR5, FR6)
# =============================================================================
@app.post("/diagrams/{diagram_id}/participants", response_model=Participant, status_code=201,
          tags=["Participants"], responses=errs(404, 409, 422))
def add_participant(diagram_id: DiagramId, body: ParticipantCreate, response: Response,
                    if_match: IfMatch = None):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        new_id = _local_id(content["participants"])
        content["participants"].append({"id": new_id, **body.model_dump(mode="json")})
        commit(d, content, response)
        return find_or_404(d["participants"], new_id, "Участник")


@app.patch("/diagrams/{diagram_id}/participants/{item_id}", response_model=Participant,
           tags=["Participants"], responses=errs(404, 409, 422))
def edit_participant(diagram_id: DiagramId, item_id: ItemId, body: ParticipantPatch,
                     response: Response, if_match: IfMatch = None):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        find_or_404(content["participants"], item_id, "Участник").update(
            body.model_dump(mode="json", exclude_unset=True))
        commit(d, content, response)
        return find_or_404(d["participants"], item_id, "Участник")


@app.delete("/diagrams/{diagram_id}/participants/{item_id}", status_code=204,
            tags=["Participants"], responses=errs(404, 409, 422))
def delete_participant(diagram_id: DiagramId, item_id: ItemId, if_match: IfMatch = None):
    """Удаляет участника и все сообщения, где он источник или получатель (FR6)."""
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        find_or_404(content["participants"], item_id, "Участник")
        content["participants"] = [p for p in content["participants"] if p["id"] != item_id]
        content["messages"] = [m for m in content["messages"]
                               if item_id not in (m["source_id"], m["target_id"])]
        commit(d, content)


# =============================================================================
# Сообщения (FR3, FR5, FR6)
# =============================================================================
@app.post("/diagrams/{diagram_id}/messages", response_model=Message, status_code=201,
          tags=["Messages"], responses=errs(404, 409, 422))
def add_message(diagram_id: DiagramId, body: MessageCreate, response: Response,
                if_match: IfMatch = None):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        data = body.model_dump(mode="json")
        if data["seq_order"] is not None:
            make_room(content["messages"], data["seq_order"])
        new_id = _local_id(content["messages"])
        content["messages"].append({"id": new_id, **data})
        commit(d, content, response)
        return find_or_404(d["messages"], new_id, "Сообщение")


@app.patch("/diagrams/{diagram_id}/messages/{item_id}", response_model=Message,
           tags=["Messages"], responses=errs(404, 409, 422))
def edit_message(diagram_id: DiagramId, item_id: ItemId, body: MessagePatch,
                 response: Response, if_match: IfMatch = None):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        msg = find_or_404(content["messages"], item_id, "Сообщение")
        patch = body.model_dump(mode="json", exclude_unset=True)
        if patch.get("seq_order") is not None:
            make_room(content["messages"], patch["seq_order"], exclude_id=item_id)
        msg.update(patch)
        commit(d, content, response)
        return find_or_404(d["messages"], item_id, "Сообщение")


@app.delete("/diagrams/{diagram_id}/messages/{item_id}", status_code=204,
            tags=["Messages"], responses=errs(404, 409, 422))
def delete_message(diagram_id: DiagramId, item_id: ItemId, if_match: IfMatch = None):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        find_or_404(content["messages"], item_id, "Сообщение")
        content["messages"] = [m for m in content["messages"] if m["id"] != item_id]
        commit(d, content)


# =============================================================================
# Фрагменты (FR4, FR5, FR6)
# =============================================================================
@app.post("/diagrams/{diagram_id}/fragments", response_model=Fragment, status_code=201,
          tags=["Fragments"], responses=errs(404, 409, 422))
def add_fragment(diagram_id: DiagramId, body: FragmentCreate, response: Response,
                 if_match: IfMatch = None):
    """Создаёт фрагмент и (необязательно) переносит в него сообщения из message_ids."""
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        new_id = _local_id(content["fragments"])
        content["fragments"].append({"id": new_id,
                                     **body.model_dump(mode="json", exclude={"message_ids"})})
        by_id = {m["id"]: m for m in content["messages"]}
        for mid in body.message_ids:
            if mid not in by_id:
                _fail(("message_ids",), f"сообщение {mid} не найдено в диаграмме")
            by_id[mid]["fragment_id"] = new_id
        commit(d, content, response)
        return find_or_404(d["fragments"], new_id, "Фрагмент")


@app.patch("/diagrams/{diagram_id}/fragments/{item_id}", response_model=Fragment,
           tags=["Fragments"], responses=errs(404, 409, 422))
def edit_fragment(diagram_id: DiagramId, item_id: ItemId, body: FragmentPatch,
                  response: Response, if_match: IfMatch = None):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        find_or_404(content["fragments"], item_id, "Фрагмент").update(
            body.model_dump(mode="json", exclude_unset=True))
        commit(d, content, response)
        return find_or_404(d["fragments"], item_id, "Фрагмент")


@app.delete("/diagrams/{diagram_id}/fragments/{item_id}", status_code=204,
            tags=["Fragments"], responses=errs(404, 409, 422))
def delete_fragment(diagram_id: DiagramId, item_id: ItemId, if_match: IfMatch = None):
    """Удаляет рамку фрагмента; вложенные фрагменты и сообщения поднимаются на уровень выше."""
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        check_version(d, if_match)
        content = content_of(d)
        parent = find_or_404(content["fragments"], item_id, "Фрагмент")["parent_id"]
        for f in content["fragments"]:
            if f["parent_id"] == item_id:
                f["parent_id"] = parent
        for m in content["messages"]:
            if m["fragment_id"] == item_id:
                m["fragment_id"] = parent
        content["fragments"] = [f for f in content["fragments"] if f["id"] != item_id]
        commit(d, content)


# =============================================================================
# Валидация (FR11): логика UML поверх структурных проверок Pydantic
# =============================================================================
@app.post("/diagrams/{diagram_id}/validate", response_model=ValidationReport,
          tags=["Operations"], responses=errs(404, 422))
def validate_diagram(diagram_id: DiagramId):
    with LOCK:
        d = copy.deepcopy(get_diagram_or_404(diagram_id))
    errors: list[str] = []

    if not d["participants"]:
        errors.append("На диаграмме нет участников")

    names = [p["name"] for p in d["participants"]]
    for n in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"Дублирующееся имя участника: «{n}»")

    for f in d["fragments"]:
        has_msgs = any(m["fragment_id"] == f["id"] for m in d["messages"])
        has_kids = any(g["parent_id"] == f["id"] for g in d["fragments"])
        if not (has_msgs or has_kids):
            errors.append(f"Фрагмент #{f['id']} ({f['type']}) пустой")
        if f["type"] in ("alt", "opt", "loop") and not f["condition"]:
            errors.append(f"Фрагмент #{f['id']} ({f['type']}): не задано guard-условие")

    # create — первое сообщение к участнику; после destroy участник не используется
    seen: set[int] = set()
    destroyed: set[int] = set()
    for m in sorted(d["messages"], key=lambda m: m["seq_order"]):
        src, dst = m["source_id"], m["target_id"]
        for pid in (src, dst):
            if pid in destroyed:
                errors.append(f"Сообщение #{m['id']}: участник #{pid} уже уничтожен (destroy)")
        if m["type"] == "create" and dst in seen:
            errors.append(f"Сообщение #{m['id']}: create должно быть первым сообщением "
                          f"к участнику #{dst}")
        if m["type"] == "destroy":
            destroyed.add(dst)
        seen.update((src, dst))

    return {"valid": not errors, "errors": errors}


# =============================================================================
# Экспорт (FR7, NF3)
# =============================================================================
ARROWS = {"sync": "->", "async": "->>", "reply": "-->", "create": "->", "destroy": "->"}


def to_plantuml(d: dict) -> str:
    parents = {f["id"]: f["parent_id"] for f in d["fragments"]}
    frag = {f["id"]: f for f in d["fragments"]}
    order = sorted(d["messages"], key=lambda m: m["seq_order"])
    created = {m["target_id"] for m in order if m["type"] == "create"}
    parts = {p["id"]: p for p in d["participants"]}

    def decl(p: dict) -> str:
        return f'{p["type"]} "{p["name"]}" as P{p["id"]}'

    lines = ["@startuml", f"title {d['name']}"]
    # создаваемые участники объявляются в момент create, остальные — сверху
    lines += [decl(p) for p in sorted(d["participants"], key=lambda p: p["position"])
              if p["id"] not in created]

    opened: list[int] = []
    declared_late: set[int] = set()
    for m in order:
        chain = ancestors(m["fragment_id"], parents)
        while opened and opened[-1] not in chain:          # закрыть лишние фрагменты
            opened.pop()
            lines.append("end")
        for fid in chain[len(opened):]:                     # открыть недостающие
            lines.append(f"{frag[fid]['type']} {frag[fid]['condition']}".rstrip())
            opened.append(fid)
        if m["type"] == "create" and m["target_id"] not in declared_late:
            lines.append("create " + decl(parts[m["target_id"]]))
            declared_late.add(m["target_id"])
        lines.append(f"P{m['source_id']} {ARROWS[m['type']]} P{m['target_id']}: {m['label']}")
        if m["type"] == "destroy":
            lines.append(f"destroy P{m['target_id']}")
    lines += ["end"] * len(opened)
    lines.append("@enduml")
    return "\n".join(lines)


def to_svg(d: dict) -> str:
    """Простой рендер без внешних зависимостей: участники, линии жизни, сообщения, рамки."""
    parts = sorted(d["participants"], key=lambda p: p["position"])
    order = sorted(d["messages"], key=lambda m: m["seq_order"])
    parents = {f["id"]: f["parent_id"] for f in d["fragments"]}
    xs = {p["id"]: 100 + i * 160 for i, p in enumerate(parts)}
    ys = {m["id"]: 120 + j * 46 for j, m in enumerate(order)}
    width = 200 + 160 * max(len(parts) - 1, 0)
    height = 120 + 46 * len(order) + 40

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
         f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="12">',
         '<defs><marker id="full" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
         'markerHeight="8" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#333"/></marker>'
         '<marker id="open" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
         'markerHeight="8" orient="auto"><path d="M0,0 L10,5 L0,10" fill="none" '
         'stroke="#333"/></marker></defs>',
         f'<rect width="{width}" height="{height}" fill="#fff"/>',
         f'<text x="{width // 2}" y="22" text-anchor="middle" font-weight="bold">'
         f'{escape(d["name"])}</text>']

    chains = {m["id"]: ancestors(m["fragment_id"], parents) for m in order}
    for f in d["fragments"]:
        inner = [ys[m["id"]] for m in order if f["id"] in chains[m["id"]]]
        if not inner:
            continue
        depth = len(ancestors(f["id"], parents)) - 1
        x1, x2 = 20 + depth * 12, width - 20 - depth * 12
        y1, y2 = min(inner) - 30 + depth * 4, max(inner) + 16
        s.append(f'<rect x="{x1}" y="{y1}" width="{x2 - x1}" height="{y2 - y1}" '
                 f'fill="none" stroke="#6672cc"/>')
        label = f["type"] + (f' [{f["condition"]}]' if f["condition"] else "")
        s.append(f'<text x="{x1 + 6}" y="{y1 + 13}" fill="#6672cc">{escape(label)}</text>')

    for p in parts:
        x = xs[p["id"]]
        s.append(f'<rect x="{x - 60}" y="40" width="120" height="30" rx="3" '
                 f'fill="#e8f0fe" stroke="#333"/>')
        s.append(f'<text x="{x}" y="59" text-anchor="middle">{escape(p["name"])}</text>')
        s.append(f'<text x="{x}" y="84" text-anchor="middle" fill="#888" font-size="10">'
                 f'«{p["type"]}»</text>')
        s.append(f'<line x1="{x}" y1="70" x2="{x}" y2="{height - 20}" stroke="#999" '
                 f'stroke-dasharray="4,4"/>')

    for m in order:
        x1, x2, y = xs[m["source_id"]], xs[m["target_id"]], ys[m["id"]]
        dash = ' stroke-dasharray="6,4"' if m["type"] == "reply" else ""
        marker = "open" if m["type"] in ("async", "reply") else "full"
        if x1 == x2:                                    # сообщение самому себе
            s.append(f'<path d="M{x1},{y - 8} h36 v16 h-36" fill="none" stroke="#333"'
                     f'{dash} marker-end="url(#{marker})"/>')
            s.append(f'<text x="{x1 + 42}" y="{y + 4}">{escape(m["label"])}</text>')
        else:
            s.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="#333"{dash} '
                     f'marker-end="url(#{marker})"/>')
            s.append(f'<text x="{(x1 + x2) // 2}" y="{y - 6}" text-anchor="middle">'
                     f'{escape(m["label"])}</text>')
        if m["type"] == "destroy":
            s.append(f'<path d="M{x2 - 7},{y + 6} l14,14 M{x2 + 7},{y + 6} l-14,14" '
                     f'stroke="#c33" stroke-width="2"/>')
    s.append("</svg>")
    return "".join(s)


def _attachment(filename: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


@app.get("/diagrams/{diagram_id}/export", tags=["Operations"],
         response_class=PlainTextResponse, responses=errs(404, 422, 501))
def export_diagram(diagram_id: DiagramId,
                   format: Annotated[Literal["puml", "svg", "png"], Query()] = "puml",
                   download: Annotated[bool, Query(description="Отдать как файл")] = False):
    with LOCK:
        d = copy.deepcopy(get_diagram_or_404(diagram_id))
    headers = _attachment(f"diagram-{diagram_id}.{format}") if download else None
    if format == "puml":
        return PlainTextResponse(to_plantuml(d), headers=headers)
    if format == "svg":
        return Response(to_svg(d), media_type="image/svg+xml", headers=headers)
    raise HTTPException(501, "PNG на сервере не рендерится: используйте format=svg "
                             "или отрендерьте format=puml на PlantUML-сервере")


# =============================================================================
# Файл проекта (FR9, FR10)
# =============================================================================
@app.get("/diagrams/{diagram_id}/file", response_model=DiagramFile, tags=["Operations"],
         responses=errs(404, 422))
def download_file(diagram_id: DiagramId):
    with LOCK:
        d = get_diagram_or_404(diagram_id)
        payload = {"format": "seq-editor", "format_version": 1, "diagram": content_of(d)}
    return JSONResponse(payload, headers=_attachment(f"diagram-{diagram_id}.seq.json"))


@app.post("/projects/{project_id}/diagrams/upload", response_model=DiagramOut,
          status_code=201, tags=["Operations"], responses=errs(404, 422))
def upload_file(project_id: ProjectId, body: DiagramFile, response: Response):
    """Тело запроса — содержимое файла, полученного из GET /diagrams/{id}/file."""
    d = create_diagram_record(project_id, body.diagram.model_dump(mode="json"))
    set_etag(response, d)
    return d


# =============================================================================
# Импорт из PlantUML (FR8)
# =============================================================================
_DECL = re.compile(r'^(actor|participant|boundary|control|entity|database)\s+'
                   r'(?:"([^"]+)"|(\w+))(?:\s+as\s+(\w+))?\s*$')
_MSG = re.compile(r'^(\w+|"[^"]+")\s*(->x(?!\w)|->>|-->|->)\s*(\w+|"[^"]+")\s*(?::\s*(.*))?$')
_FRAG = re.compile(r"^(alt|opt|loop|par|critical)\b\s*(.*)$")
_UNSUPPORTED_BLOCKS = {"group", "break", "ref", "box"}
_IGNORED_WORDS = {"skinparam", "autonumber", "hide", "show", "activate", "deactivate"}
_IGNORED_SYMBOLS = ("!", "==", "...", "||")
_ARROW_TYPE = {"->": "sync", "->>": "async", "-->": "reply", "->x": "destroy"}


def parse_plantuml(code: str) -> tuple[dict, list[str]]:
    participants: list[dict] = []
    fragments: list[dict] = []
    messages: list[dict] = []
    warnings: list[str] = []
    by_alias: dict[str, int] = {}
    by_name: dict[str, int] = {}
    stack: list[Optional[int]] = []        # None — неподдерживаемый блок (group и т. п.)
    pending_create: set[int] = set()
    title: Optional[str] = None
    in_note = False

    def declare(ptype: str, name: str, alias: Optional[str]) -> int:
        pid = len(participants) + 1
        participants.append({"id": pid, "name": name, "type": ptype, "position": pid - 1})
        by_alias[alias or name] = pid
        by_name.setdefault(name, pid)
        return pid

    def resolve(token: str) -> int:
        if token.startswith('"'):
            name = token.strip('"')
            return by_name.get(name) or declare("participant", name, None)
        return by_alias.get(token) or by_name.get(token) or declare("participant", token, token)

    def current_fragment() -> Optional[int]:
        return next((f for f in reversed(stack) if f is not None), None)

    for n, raw in enumerate(code.splitlines(), start=1):
        line = raw.strip()
        low = line.lower()
        head = low.split(maxsplit=1)[0] if low else ""   # первое слово строки
        if in_note:
            in_note = not low.startswith("end note")
            continue
        if not line or line.startswith("'") or line.startswith("@"):
            continue
        if head in ("note", "hnote", "rnote"):
            in_note = ":" not in line                    # многострочная заметка до "end note"
            continue
        if head == "title":
            title = line[5:].strip() or title
            continue
        if head in _IGNORED_WORDS or low.startswith(_IGNORED_SYMBOLS):
            continue

        if low.startswith("create "):
            rest = line[7:].strip()
            m = _DECL.match(rest)
            pid = declare(m[1], m[2] or m[3], m[4]) if m else resolve(rest)
            pending_create.add(pid)
            continue
        if low.startswith("destroy "):
            pid = resolve(line[8:].strip())
            last = messages[-1] if messages else None
            if last and last["target_id"] == pid:
                last["type"] = "destroy"
            else:
                warnings.append(f"строка {n}: destroy без предшествующего сообщения к участнику — пропущено")
            continue

        if m := _DECL.match(line):
            declare(m[1], m[2] or m[3], m[4])
        elif m := _MSG.match(line):
            src, dst = resolve(m[1]), resolve(m[3])
            mtype = _ARROW_TYPE[m[2]]
            if dst in pending_create:
                mtype = "create"
                pending_create.discard(dst)
            messages.append({"id": len(messages) + 1, "source_id": src, "target_id": dst,
                             "type": mtype, "label": (m[4] or "").strip(),
                             "fragment_id": current_fragment(), "seq_order": len(messages)})
        elif m := _FRAG.match(line):
            fid = len(fragments) + 1
            fragments.append({"id": fid, "type": m[1], "condition": m[2].strip(),
                              "parent_id": current_fragment(), "seq_order": fid - 1})
            stack.append(fid)
        elif head in _UNSUPPORTED_BLOCKS:
            warnings.append(f"строка {n}: блок «{line.split()[0]}» не поддерживается, "
                            f"содержимое отнесено к внешнему фрагменту")
            stack.append(None)
        elif head == "else":
            warnings.append(f"строка {n}: ветки else не поддерживаются моделью — "
                            f"сообщения отнесены к тому же фрагменту")
        elif low == "end":
            if not stack:
                _fail(("plantuml", f"строка {n}"), "лишний end — нет открытого фрагмента")
            stack.pop()
        else:
            warnings.append(f"строка {n}: не распознано — «{line[:60]}»")

    if stack:
        _fail(("plantuml",), f"не закрыто фрагментов: {len(stack)} (нет end)")
    for pid in pending_create:
        warnings.append(f"create для участника #{pid} без сообщения — пропущено")

    return ({"name": title or "Импортированная диаграмма", "participants": participants,
             "fragments": fragments, "messages": messages}, warnings)


@app.post("/projects/{project_id}/import", response_model=ImportOut, status_code=201,
          tags=["Operations"], responses=errs(404, 422))
def import_plantuml(project_id: ProjectId, body: ImportIn):
    with LOCK:
        get_project_or_404(project_id)                 # 404 раньше разбора текста
    content, warnings = parse_plantuml(body.plantuml)
    if body.name:
        content["name"] = body.name
    return {"diagram": create_diagram_record(project_id, content), "warnings": warnings}


# =============================================================================
# Демо-данные (только при пустом хранилище и SEED_DEMO=1)
# =============================================================================
def seed_demo() -> None:
    with LOCK:
        p = {"id": _new_id("project"), "name": "Демо-проект", "description": "Создан при запуске"}
        projects[p["id"]] = p
    create_diagram_record(p["id"], {
        "name": "Авторизация",
        "participants": [{"id": 1, "name": "Пользователь", "type": "actor"},
                         {"id": 2, "name": "Сервер", "type": "control"},
                         {"id": 3, "name": "БД", "type": "database"}],
        "fragments": [{"id": 1, "type": "alt", "condition": "логин верный"}],
        "messages": [{"id": 1, "source_id": 1, "target_id": 2, "label": "login()"},
                     {"id": 2, "source_id": 2, "target_id": 3, "label": "findUser()"},
                     {"id": 3, "source_id": 3, "target_id": 2, "type": "reply", "label": "user"},
                     {"id": 4, "source_id": 2, "target_id": 1, "type": "reply",
                      "label": "token", "fragment_id": 1}],
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)