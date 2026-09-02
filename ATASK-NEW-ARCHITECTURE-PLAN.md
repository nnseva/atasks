# План: изоляция RPC-запросов по зарегистрированным atask

## 0. Проблема (кратко)

Сейчас RPC-очередь запросов (`@atask`) одна на namespace, биндится маской
`<prefix>.r.#`, и все инстансы, вызвавшие `Router.activate()`, становятся
конкурирующими консьюмерами этой одной очереди — независимо от того, какой
конкретно набор `@atask` у них зарегистрирован. Запрос, ушедший инстансу без
нужного обработчика, приводит к `JobNotFound` и теряется безвозвратно
(сообщение уже подтверждено брокеру раньше, чем стало известно, есть ли
обработчик — см. `atasks/transport/backends/amqp.py`, `register_callback` /
`_on_message`).

Решение: одна очередь запросов **на каждый зарегистрированный `@atask`**
(а не на инстанс), по аналогии с тем, как уже сделано для `@atask_queue`
(`register_event_callback`). Инстанс, не зарегистрировавший данный `atask`,
физически не подписан на его очередь и никогда не получит для неё
сообщение.

Сопутствующее решение: регистрация `@atask` /`@atask_queue`/
`@atask_broadcast` возможна только до вызова `Router.activate()`. После
`activate()` регистрация запрещена (бросает исключение), а не молча
игнорируется — так и исходный сценарий гонки с регистрацией "на лету"
снимается, и ошибка порядка инициализации в пользовательском коде сразу
видна.

Обратная совместимость и миграция очередей на сервере AMQP не
рассматриваются — у пакета пока нет внедрений.

---

## 1. `atasks/registry.py`

1.1. Добавить в `Manager` метод перечисления зарегистрированных имён:

```python
def names(self):
    """Names of all currently registered items, in registration order"""
    return list(self._registry.keys())
```

`RegistryItem` не меняется.

1.2. `Manager` как класс не меняется иначе — разделение по видам (`rpc` /
`queue` / `broadcast`) делается снаружи, тремя отдельными экземплярами
`Manager`, а не тегом внутри записи (см. п.2).

---

## 2. `atasks/router.py`

### 2.1. Три реестра вместо одного

`Router.__init__` сейчас регистрирует в `namespaces` один `registry`.
Нужно завести три независимых `Manager(namespace, unite=False)`:
`rpc_registry`, `queue_registry`, `broadcast_registry`.

```python
namespaces.register(
    namespace,
    router=self,
    rpc_registry=Manager(namespace, unite=False),
    queue_registry=Manager(namespace, unite=False),
    broadcast_registry=Manager(namespace, unite=False),
)
```

Все места, читавшие `namespaces.get(namespace).registry`, переключить на
нужный конкретный реестр:

- `register_atask` / `_on_request` → `rpc_registry`
- `register_atask_queue` / `_on_event` → `queue_registry`
- `register_atask_broadcast` / `_on_broadcast` → `broadcast_registry`

### 2.2. Запрет регистрации после активации

Новое исключение (рядом с `JobNotFound` и т.п.):

```python
class LateRegistration(Exception):
    """atask/atask_queue/atask_broadcast registered after Router.activate()"""
```

В начале `register_atask`, `register_atask_queue`, `register_atask_broadcast`:

```python
if self.server is not None:
    raise LateRegistration(name)
```

(`self.server` — тот же признак активности, что и сейчас; отдельный флаг не
нужен.)

### 2.3. `activate()` / `deactivate()` — единственная публичная точка входа

Публичные `activate_queue` / `deactivate_queue` / `activate_broadcast` /
`deactivate_broadcast` убираются как самостоятельный пользовательский API.
Их работу берёт на себя `activate()`/`deactivate()`, перебирая все три
реестра. Поимённая активация конкретного вида (request/queue/broadcast)
становится защищённым (`_`) деталем реализации `Router`, вызываемым только
из `activate()`/`deactivate()`:

```python
async def activate(self, server):
    if self.server == server:
        return
    if self.server:
        await self.deactivate()
    self.server = server
    if not self.server:
        return

    ns = namespaces.get(self.namespace)
    names = {
        'rpc': ns.rpc_registry.names(),
        'queue': ns.queue_registry.names(),
        'broadcast': ns.broadcast_registry.names(),
    }
    for name in names['rpc']:
        await self._activate_request(name)
    for name in names['queue']:
        await self._activate_queue(name)
    for name in names['broadcast']:
        await self._activate_broadcast(name)
    self._activated = names

async def deactivate(self):
    if not self.server:
        return
    for name in self._activated['rpc']:
        await self._deactivate_request(name)
    for name in self._activated['queue']:
        await self._deactivate_queue(name)
    for name in self._activated['broadcast']:
        await self._deactivate_broadcast(name)
    self._activated = {'rpc': [], 'queue': [], 'broadcast': []}
    self.server = None
```

`deactivate()` отключает ровно тот снимок имён, который был активирован —
а не текущее содержимое реестров, которое (в норме) уже не должно меняться
после `activate()`, но так деактивация остаётся корректной даже при
нарушении этой конвенции.

Защищённые поимённые помощники (симметричны для трёх видов, показан один):

```python
async def _activate_request(self, name):
    async def _callback(content):
        return await self._on_request(name, content)
    await self.server._register_request_callback(name, _callback)

async def _deactivate_request(self, name):
    await self.server._unregister_request_callback(name)
```

`_activate_queue`/`_deactivate_queue` и `_activate_broadcast`/
`_deactivate_broadcast` — те же обёртки над `_on_event`/`_on_broadcast` и
`server._register_event_callback`/`_register_broadcast_callback` (сигнатуры
transport-методов подгоняются под единый паттерн, см. §3).

### 2.4. `_on_request`/`_on_event`/`_on_broadcast`

Меняется только источник поиска обработчика (конкретный реестр вместо
общего `.registry`, см. 2.1). Остальная логика (`_call_coro`, трассировка,
кодирование ответа) не меняется — в частности, **сознательно сохраняется**
подтверждение сообщения брокером до вызова обработчика (риск блокировки
сети обработчиков при рекурсивных вызовах важнее, чем эта конкретная
потеря сообщения при ошибке в обработчике; отдельная тема, не в этом
плане).

---

## 3. `atasks/transport/base.py`

### 3.1. `Transport` — единый защищённый паттерн для всех трёх видов

Убрать `register_callback`/`unregister_callback` (единый глобальный
callback больше не нужен — `Router` не работает с транспортом напрямую по
этому контракту). Ввести/переименовать симметричную четвёрку методов,
все — защищённые (вызываются только из `Router`, не пользователем):

- `_register_request_callback(name, callback)` / `_unregister_request_callback(name)` — **новые**
- `_register_event_callback(name, callback)` / `_unregister_event_callback(name)` — переименование текущих `register_event_callback`/`unregister_event_callback`
- `_register_broadcast_callback(name, callback)` / `_unregister_broadcast_callback(name)` — переименование текущих `register_broadcast_callback`/`unregister_broadcast_callback`

Контракт `callback` для всех трёх одинаковый по форме — принимает
raw-content (`bytes`); отличие только в возвращаемом значении:
event/broadcast — ничего не возвращают (fire-and-forget), request —
обязаны вернуть закодированный ответ (`bytes`), который транспорт
отправит вызывающей стороне.

`send_request`, `publish_event`, `publish_broadcast`, `connect`,
`disconnect` — остаются публичными как сейчас, это операции отправки, а не
активации подписки.

### 3.2. `LoopbackTransport`

Явно НЕ обязан повторять поведение `AMQPTransport` — по замыслу это
максимально простая тривиальная реализация транспорта для тестов, а не
эталонная имитация брокера. Единственное обязательное требование — тот же
интерфейс, что видит `Router` (методы из 3.1).

- `self.callback` (единственный глобальный) заменяется словарём
  `self._request_callbacks = {}`, по аналогии с уже существующими
  `self._event_callbacks`/`self._broadcast_callbacks`.
- `_register_request_callback`/`_unregister_request_callback` — тривиальная
  работа со словарём.
- `send_request(name, content, timeout=None)`: ищет callback по имени в
  словаре. Если не найден — **не** имитируется поведение реального брокера
  (тихое зависание/потеря сообщения); вместо этого сразу бросается явная
  ошибка (новое исключение, например `UnknownRequestName`). Это
  сознательное расхождение с `AMQPTransport`: `LoopbackTransport` остаётся
  "простейшим транспортом", у будущих транспортов может быть любое своё
  поведение при обращении к незарегистрированному имени (в т.ч. быстрая
  ошибка "онлайн", если транспорт это умеет проверить) — реального
  контракта здесь `Transport` не навязывает.
- Убрать сохранившийся сейчас `except Exception as ex: logger.error(...)`
  вокруг вызова callback в `send_request` — при простейшей реализации
  исключение из обработчика должно долетать до вызывающего кода, а не
  тихо превращаться в `None`/`TransportError` на уровне `Router`.

---

## 4. `atasks/transport/backends/amqp.py`

### 4.1. Именование очередей — единое правило (поправка)

**Поправка к первоначальному варианту плана.** Все именованные очереди
строятся по одному и тому же шаблону, включающему оба параметра
конструктора, а не один вместо другого:

```
<self.queue>.<self.prefix>.<дальше — как было раньше>
```

То есть имя очереди всегда начинается с `self.queue`, затем идёт
`self.prefix`, а дальше — тот же "хвост", что и в уже существующей схеме
(буква вида + `name`). `self.prefix` при этом как и раньше используется
и в routing-key namespace обмена (`.r.`/`.e.`/`.b.`) — это не меняется,
он просто дополнительно входит и в имя очереди.

Заодно переименовать атрибут конструктора: сейчас параметр называется
`queue=`, но хранится как `self.queue_name` — привести к `self.queue`,
по аналогии с `self.prefix = prefix` (имя атрибута = имя параметра).

Анонимные эксклюзивные очереди (очередь ответов `_response_queue` и
эксклюзивная очередь `@atask_broadcast`-подписчика) правило не затрагивает
— у них нет собственного имени вообще, шаблон к ним неприменим.

Итоговые имена очередей:

| вид | имя очереди | routing key биндинга |
|---|---|---|
| RPC-запрос, на каждый `name` | `<queue>.<prefix>.r.<name>` | `<prefix>.r.<name>` (точный, не маска) |
| `@atask_queue`, на каждый `name` | `<queue>.<prefix>.q.<name>` | `<prefix>.e.<name>` |
| `@atask_broadcast` | анонимная, эксклюзивная, auto-delete | `<prefix>.b.<name>` |
| ответы RPC | анонимная, эксклюзивная | — (routing key = имя самой очереди) |

Буква `.r.` для RPC-очереди выбрана по аналогии с её же routing-key
префиксом — прежде для RPC отдельной именной очереди не было (была одна
общая `self.queue_name` без буквы вида), так что здесь нет буквального
"как было раньше", это единственное новое решение в этой таблице; если
буква не нравится — заменить проще всего именно здесь, до реализации.

### 4.2. Удалить общую RPC-очередь и маску

Убрать из `register_callback`/`_on_message` весь текущий код: единственную
очередь `self._queue`/`self._consumer`, биндинг маской
`self._request_routing_prefix + '#'`, метод `register_callback`/
`unregister_callback` целиком (заменяются методами ниже).

### 4.3. Новые методы — `_register_request_callback`/`_unregister_request_callback`

По аналогии с уже существующими `register_event_callback`/
`unregister_event_callback`, но с ответом:

```python
async def _register_request_callback(self, name, callback):
    await self._lock.acquire()
    try:
        queue_name = '%s.%s.r.%s' % (self.queue, self.prefix, name)
        queue = await self._channel.declare_queue(queue_name, durable=True)
        await queue.bind(self._request_exchange, self._request_routing_prefix + name)

        async def _on_message(message):
            async with message.process():
                info = message.info()
                request = message.body
            correlation_id = info['correlation_id']
            logger.info('Got request for %s[%s]', name, correlation_id)
            try:
                response = await callback(request)
            except Exception:
                logger.exception('Unhandled error handling request for %s[%s]', name, correlation_id)
                return
            try:
                await self._response_exchange.publish(
                    aio_pika.Message(correlation_id=correlation_id, body=response),
                    routing_key=info['reply_to'],
                )
            except Exception as exc:
                logger.exception('Failed to publish response for %s[%s]', name, correlation_id)
                self._fail_awaiting_requests(exc)

        consumer_tag = await queue.consume(_on_message)
        self._request_queues[name] = queue
        self._request_consumers[name] = consumer_tag
    finally:
        self._lock.release()

async def _unregister_request_callback(self, name):
    await self._lock.acquire()
    try:
        queue = self._request_queues.pop(name, None)
        consumer_tag = self._request_consumers.pop(name, None)
        if queue is not None and consumer_tag is not None \
                and hasattr(self, '_connection') and not self._connection.is_closed:
            await queue.cancel(consumer_tag)
    finally:
        self._lock.release()
```

`name` больше не нужно вынимать из `info['routing_key']` — очередь и так
однозначно соответствует одному имени (замыкание). `routing_key` можно
оставить в логах при желании, но как источник истины он не нужен.

### 4.4. Точки данных и очистка

- В `__init__`: добавить `self._request_queues = {}`,
  `self._request_consumers = {}` (по образцу `_event_queues`/
  `_event_consumers`).
- В `disconnect()`: добавить `self._request_queues.clear()`,
  `self._request_consumers.clear()` рядом с очисткой event/broadcast
  словарей — `disconnect` обязан убирать всё, что создано в `connect`
  (и в `_register_request_callback`, вызванном после).
- В `register_event_callback`/`register_broadcast_callback` (переименовать
  в `_register_event_callback`/`_register_broadcast_callback`, см. §3.1) —
  логика не меняется, только имя метода и имя очереди события, приведённое
  к шаблону `<self.queue>.<self.prefix>.q.<name>` из §4.1
  (см. 4.1).

---

## 5. `atasks/run.py`

**Критично**: сейчас `aiomain()` вызывает `router.activate(transport)`
_до_ загрузки модулей сценария (`scenario`), а именно в них лежат
`@atask`/`@atask_queue`/`@atask_broadcast`. После этого рефакторинга такой
порядок не просто бессмысленен, а прямо ломается: `activate()` увидит
пустые реестры, а любая регистрация в загружаемых следом модулях упадёт с
`LateRegistration`.

Нужно поменять порядок в `aiomain()`:

1. подключить transport (`await transport.connect()`) — как сейчас;
2. импортировать все модули `scenario` (текущий блок
   `for filename in options['scenario']: ...`) — переместить **раньше**
   активации;
3. только после этого — `await router.activate(transport)` (если
   `mode in ('server', 'loopback')`);
4. дальше — ожидание сигналов/`futures`, как сейчас.

Обратить внимание: `futures` (результат `module.aiomain(**options)` для
модулей сценария) сейчас собираются в том же проходе, где модули
импортируются, и запускаются (`asyncio.gather`) уже после `activate()`.
Нужно сохранить эту сборку `futures` при переносе блока импорта выше, но
сам `await asyncio.gather(*futures)` — оставить после `activate()`, как
сейчас (если только `module.aiomain` сам не регистрирует что-то новое, что
не должно быть типичным случаем).

---

## 6. `README.md` + отдельный документ по топологии AMQP

- Явно описать новое правило: `Router.activate()` вызывается один раз,
  после того как отработали все декораторы `@atask`/`@atask_queue`/
  `@atask_broadcast`, которые должны обслуживаться этим инстансом; попытка
  зарегистрировать что-либо после `activate()` — ошибка (`LateRegistration`),
  а не молчаливое игнорирование.
- Убрать/переписать текущее описание `activate_queue`/`deactivate_queue`/
  `activate_broadcast`/`deactivate_broadcast` как самостоятельного
  пользовательского API (строки ~386–396, ~421–423 текущего README) — они
  больше не вызываются пользователем напрямую, всё это делает `activate()`/
  `deactivate()`.
- Топология AMQP (имена очередей, routing-key namespace, что изменится в
  RabbitMQ management UI — вместо одной очереди `atask` появится очередь на
  каждый зарегистрированный `@atask`/`@atask_queue`) — вынести в отдельный
  документ по образцу `TRACE-ATASK-STACK.md`, например
  `AMQP-TRANSPORT-TOPOLOGY.md`, и сослаться на него из раздела README,
  посвящённого именно `AMQPTransport` (общий `Router`/`Transport`-API этот
  документ не касается — топология специфична для AMQP-транспорта).

---

## 7. Тесты (`dev/tests/`)

Пройти и привести в соответствие с новым API:

- `test_001_registry.py` — добавить проверку `Manager.names()`.
- `test_003_transport.py`, `test_005_command.py` — проверить использование
  `Transport`/`LoopbackTransport` (переименованные защищённые методы,
  словарь `_request_callbacks`, ошибка при неизвестном имени вместо
  зависания/`None`).
- `test_004_router.py` — `activate()`/`deactivate()` теперь без единого
  `register_callback`; добавить тест на `LateRegistration` при регистрации
  после `activate()`; убедиться, что `deactivate()` откатывает ровно снятый
  на момент `activate()` снимок имён.
- `test_006_amqp_rpc.py`, `test_007_amqp_queue.py`,
  `test_008_amqp_broadcast.py`, `test_010_amqp_reconnect.py`,
  `test_012_amqp_coverage.py` — привести к новым именам очередей/методов;
  проверить, что реконнект `aio_pika` корректно восстанавливает N очередей
  запросов, а не одну.
- `test_009_run_shutdown.py` — проверить новый порядок операций в
  `run.aiomain` (импорт сценария до `activate()`).
- **Новый обязательный регрессионный тест** (реального бага, ради которого
  всё затевалось): два `AMQPTransport`, подключённые к одному брокеру, с
  непересекающимися наборами зарегистрированных `@atask`. Утверждение:
  запрос к имени, которого нет у транспорта A, никогда не обрабатывается A
  (а обрабатывается B), сколько раз тест ни повторить — а не "статистически
  редко попадает не туда". Разместить рядом с `test_006_amqp_rpc.py` (можно
  прямо в нём, отдельным тестом, либо в новом файле
  `test_013_amqp_rpc_isolation.py`).

---

## 8. Порядок реализации

1. `registry.py` — `Manager.names()`.
2. `transport/base.py` — новый защищённый 4-методный контракт на
   `Transport`; `LoopbackTransport` на его основе.
3. `transport/backends/amqp.py` — очереди на каждый `name` для RPC;
   унификация именования очередей через `queue_name`; очистка в
   `disconnect()`.
4. `router.py` — три реестра, `LateRegistration`, переработанные
   `activate()`/`deactivate()`, удаление публичных `activate_queue` и т.п.
5. `run.py` — порядок импорта сценария относительно `activate()`.
6. Тесты — по каждому пункту выше, плюс регрессионный тест изоляции.
7. `README.md` + новый `AMQP-TRANSPORT-TOPOLOGY.md`.
