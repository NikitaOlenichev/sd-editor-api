# Sequence Diagram Editor API

REST API редактора UML-диаграмм последовательности на FastAPI: проекты, диаграммы, участники, сообщения и комбинированные фрагменты, валидация, экспорт в PlantUML/SVG и импорт из PlantUML.

## Запуск

**Локально** (Python 3.10+):

```bash
pip install -r requirements.txt
python main.py
```

Swagger UI: <http://127.0.0.1:9000/docs>

**В Docker:**

```bash
docker compose up --build
```

Swagger UI: <http://localhost:8080/docs>

## Модель данных

```
Project 1 ── * Diagram
Diagram 1 ── * Participant   actor | participant | boundary | control | entity | database
Diagram 1 ── * Fragment      alt | opt | loop | par | critical  (вложенность через parent_id)
Diagram 1 ── * Message       sync | async | reply | create | destroy
```

## Маршруты

| Группа | Маршруты |
|---|---|
| Сервис | `GET /health` |
| Проекты | `GET, POST /projects` · `GET, PUT, PATCH, DELETE /projects/{id}` |
| Диаграммы | `GET, POST /projects/{id}/diagrams` · `GET, PUT, PATCH, DELETE /diagrams/{id}` |
| Элементы | `POST /diagrams/{id}/{participants\|messages\|fragments}` · `PATCH, DELETE …/{item_id}` |
| Операции | `POST /diagrams/{id}/validate` · `GET /diagrams/{id}/export?format=puml\|svg` |
| Файлы | `POST /projects/{id}/import` (PlantUML) · `GET /diagrams/{id}/file` · `POST /projects/{id}/diagrams/upload` |

Полное описание с примерами — в Swagger UI.

## Ошибки

```json
404 / 409 / 501  {"error": "not_found", "detail": "Диаграмма #42 не найдена"}
422              {"error": "validation_error", "details": [{"field": "name", "message": "..."}]}
```

409 возвращается, если в заголовке `If-Match` указана устаревшая версия диаграммы (текущая приходит в `ETag`).

## Настройки

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PORT` | `9000` | порт при запуске через `python main.py` |
| `STORAGE_DIR` | `storage` | папка для `state.json` |
| `AUTOSAVE_SEC` | `30` | интервал автосохранения, с |
| `SEED_DEMO` | `1` | создать демо-проект при пустом хранилище |
| `CORS_ORIGINS` | `*` | разрешённые источники через запятую |

Данные хранятся в памяти и сохраняются в `storage/state.json` каждые 30 секунд и при остановке сервера. Чтобы начать с чистого листа, удалите папку `storage`.