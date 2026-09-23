# Career Quest — датасет

Синтетические данные. Все люди и компании вымышлены.

**Дата среза данных:** `2026-10-01`. Считайте эту дату «сегодняшней».
**Период истории:** `2024-10-01` – `2026-09-30`.

## Файлы

| Файл | Содержимое | Размер |
|---|---|---|
| `skills.json` | Каталог навыков, шкала владения и требования к ролям по грейдам | 60 навыков, 8 ролей × 4 грейда |
| `employees.json` | Профили сотрудников | 200 |
| `events.json` | Каталог развивающих мероприятий | 40 |
| `activity_history.csv` | Журнал участия | 2 743 записи |

## Связи

```text
employees.skills ─────────────┐
role_profiles.required_skills ├──> skills.skill_id
events.develops_skills ───────┤
events.prerequisites ─────────┘
employees.(role, grade) ──────> role_profiles.(role, grade)
employees.manager_id ─────────> employees.employee_id
activity_history.employee_id ─> employees.employee_id
activity_history.event_id ────> events.event_id
```

Все ссылки корректны. Идентификаторы уникальны.

## skills.json

`proficiency_scale` — описание уровней владения от 0 до 5. Все уровни навыков в датасете используют эту шкалу.

`skills[]`

| Поле | Тип | Примечания |
|---|---|---|
| `skill_id` | string | Например, `SK_SYSTEM_DESIGN` |
| `name` | string | |
| `type` | `hard` \| `soft` | |
| `category` | string | Группировка для отчётов |
| `description` | string | |

`role_profiles[]` — по одной записи для каждой комбинации роли и грейда.

| Поле | Тип | Примечания |
|---|---|---|
| `role` | string | 8 ролей |
| `grade` | `Junior` \| `Middle` \| `Senior` \| `Lead` | В указанном порядке |
| `required_skills` | object | `skill_id → минимальный уровень` для этого грейда |
| `critical_skills` | array | Навыки, которые обязательно должны соответствовать требованиям для данного грейда. Ключевой фактор для повышения |

Требования не снижаются при переходе от одного грейда к следующему.

## employees.json

| Поле | Тип | Примечания |
|---|---|---|
| `employee_id` | string | `E0001` … `E0200` |
| `full_name` | string | Вымышленное имя |
| `department` | string | Один отдел на каждую роль |
| `role`, `grade` | string | Соответствуют записи в `role_profiles` |
| `manager_id` | string \| null | Сотрудник грейда `Lead` из того же отдела. `null` — для руководителей отделов |
| `hire_date` | date | |
| `tenure_months` | int | Количество полных месяцев от `hire_date` до даты среза данных |
| `work_format` | `office` \| `hybrid` \| `remote` | |
| `preferred_language` | `kk` \| `ru` \| `en` | Предпочтительный язык интерфейса |
| `career_goal` | object \| null | `{target_role, target_grade}`. `null` означает, что цель не задана |
| `skills` | object | `skill_id → уровень 0–5`. Отсутствующий навык считается равным уровню 0 |
| `last_review_date` | date | Дата последней оценки навыков |

Уровни навыков отражают результаты последней оценки. Мероприятия, завершённые после `last_review_date`, ещё не учтены.

## events.json

| Поле | Тип | Примечания |
|---|---|---|
| `event_id` | string | `EV_001` … `EV_040` |
| `title`, `description` | string | |
| `type` | string | `compliance`, `onboarding`, `course`, `workshop`, `mentoring`, `certification`, `meetup` |
| `format` | `online` \| `offline` \| `self_paced` | |
| `duration_hours` | number | Общие трудозатраты |
| `mandatory` | bool | Назначено HR. Не является объектом для рекомендаций |
| `target_roles`, `target_grades` | array | Для каких ролей и грейдов предназначено мероприятие |
| `develops_skills` | array | `{skill_id, gain, max_level}`: завершение повышает уровень навыка на `gain`, но не выше `max_level`. Для комплаенс-обучения массив пуст |
| `prerequisites` | object | `skill_id → минимальный уровень`, необходимый для участия |
| `upcoming_sessions` | array of dates | Даты будущих сессий. Пустой массив для `self_paced`, поскольку они доступны в любое время |

## activity_history.csv

Одна строка — участие одного сотрудника в одном мероприятии.

| Столбец | Тип | Примечания |
|---|---|---|
| `record_id` | string | `R000001` … |
| `employee_id`, `event_id` | string | |
| `date` | date | Дата сессии; для `self_paced` — дата зачисления или назначения |
| `due_date` | date \| empty | Только для обязательных мероприятий |
| `status` | string | См. ниже |
| `completion_pct` | int 0–100 | |
| `score` | int 0–100 \| empty | Итоговая оценка. Только для курсов, сертификаций и комплаенс-обучения |
| `feedback_rating` | int 1–5 \| empty | Оценка мероприятия сотрудником. Необязательное поле |
| `assigned_by` | `self` \| `manager` \| `hr` | Кто инициировал участие |

**Статусы**

| Статус | Значение | `completion_pct` |
|---|---|---|
| `completed` | Завершено | 100 |
| `in_progress` | Начато, но ещё не завершено | 0–95 |
| `dropped` | Начато и прекращено | 5–95 |
| `no_show` | Сотрудник зарегистрировался на сессию, но не пришёл. Только для мероприятий по расписанию | 0 |
| `declined` | Сотрудник отказался от назначения руководителя или HR | 0 |
| `overdue` | Обязательное мероприятие не завершено до `due_date` | 0–95 |

Строки отсортированы по `date`, `employee_id`, `event_id`.

## Правила

- После статуса `completed` мероприятие не повторяется. Исключение: `EV_036` — регулярный клуб.
- Добровольные мероприятия в истории всегда соответствуют роли и грейду сотрудника — текущему или предыдущему — и требованиям к участникам.
- Новые сотрудники проходят `EV_004` в течение первого месяца работы.
- Для оценки используются дополнительные профили сотрудников и записи истории в том же формате. Решение должно уметь загружать их.
