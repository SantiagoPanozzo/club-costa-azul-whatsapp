# KAN-242 — Eficiencia de mensajes

Implementación local de la historia ampliada a todos los flujos consultivos del bot.
No modifica templates ni el broadcast. Los cambios de UX y la metodología fueron
acordados con el usuario antes de implementar.

## Medición reproducible

```bash
uv run pytest tests/test_message_budgets.py -v
uv run pytest tests/
uv run ruff check .
uv run ruff format --check .
```

El baseline auditado está fijado en `tests/fixtures/message_baseline.json`, commit
`0233f2f` (develop, con KAN-271). Los tests actuales pasan por el router, el wrapper
de almacenamiento y la normalización del payload de Meta; todas las operaciones
externas están mockeadas. No envían mensajes a socios.

Se cuenta desde elegir la categoría en un menú ya visible, con socio identificado,
hasta el resultado y menú final. Una lista o mensaje de botones cuenta como un
mensaje, no como varios por sus opciones. Abrir visualmente una lista no agrega un
mensaje de usuario. La identificación inicial se mide por separado.

| Tarea | Baseline | Actual normal | Ahorro |
|---|---:|---:|---:|
| Crear reserva | 10 | 7 | 3 (30 %) |
| Cancelar reserva | 5 | 4 | 1 (20 %) |
| Inscripción a clase, una elegible | 7 | 3 | 4 (57,1 %) |
| Inscripción a clase, varias elegibles | 7 | 4 | 3 (42,9 %) |
| Inscripción general, sin clases programadas | 6 | 3 | 3 (50 %) |
| Baja de clase o inscripción general | 6 | 4 | 2 (33,3 %) |
| Inscripción a evento | 7 | 3 | 4 (57,1 %) |
| Baja de evento | 5 | 4 | 1 (20 %) |
| Comprobante, una cuota pendiente | 6 | 2 | 4 (66,7 %) |
| Comprobante, varias cuotas pendientes | 6 | 3 | 3 (50 %) |
| Datos personales / comunicados | 3 | 1 | 2 (66,7 %) |
| Consulta de cuotas sin pendientes / bienvenida | 2 | 1 | 1 (50 %) |
| Ayuda | 1 | 1 | 0 |

Los recorridos normales son presupuestos deterministas, **no medianas observadas
en producción ni una factura**. Paginación, consultas opcionales al estado completo,
recuperación de errores, cambios de condiciones y overflow necesario se miden aparte.
El usuario eligió no esperar veinte ejecuciones reales de baseline antes de entregar:
no se debe afirmar que se cumplió el criterio original de mediana histórica antes/después.

## Comportamiento

- Reservas abre espacios y Mis reservas en la misma lista. Inicio/fin se ingresan
  juntos en formato de 24 horas, con separadores habituales. Se rechazan rangos
  ambiguos, datos extra, horas inválidas y cruces de medianoche. Personas y motivo
  siguen separados. Se conserva el filtrado de espacios no reservables y la
  revalidación de precio/capacidad antes de crear.
- Actividades y eventos abren catálogos con Mis inscripciones. Los resúmenes usan
  el máximo detalle que cabe: completo, última inscripción por fecha de alta más
  cantidad restante, o solo cantidad. El historial completo permanece consultable.
  Una fila histórica nunca debe ocultar la baja puntual de una clase activa.
- Una única clase llega a confirmación directamente. KAN-271 sigue impidiendo el
  fallback genérico cuando existen clases programadas llenas/cerradas/cubiertas.
  La baja de una clase no cancela la inscripción general.
- Las descripciones completas de eventos se muestran con botones cuando caben;
  si no, se envían aparte solo cuando es necesario, conservando datos críticos en
  la confirmación. No se habilitan controles después de un overflow cuyo envío no
  fue confirmado por la API.
- Cuotas elimina la pregunta de intención de carga. Una única cuota pendiente
  muestra período, importe e instrucciones directamente. Ver estado completo no
  altera la asociación elegida. Si el importe/período cambia antes de la carga,
  el socio debe revisar y reenviar el archivo; no se atribuye silenciosamente.
- Las confirmaciones ofrecen Confirmar (o equivalente), Volver y Menú principal.
  Volver retorna al selector anterior; Menú principal abandona por elección
  explícita. Las operaciones completadas reciben resultado y menú juntos.
- El ID de confirmación incluye una versión aleatoria de un solo uso. Botones de
  otra selección o de condiciones anteriores no autorizan una escritura nueva.
- Errores recuperables vuelven a mostrar las opciones en un mensaje. No se agrega
  un texto de aviso separado. Las listas pagan navegación con filas reservadas,
  nunca truncando silenciosamente elementos inaccesibles.

## Límites y envío

- Texto: 4096 caracteres; cuerpo con botones: 1024; máximo tres botones.
- Listas: diez filas totales, títulos de 24 caracteres y descripciones de 72.
- El cuerpo de lista usa por defecto **1024**, conservador para la versión `v20.0`
  existente. `WHATSAPP_LIST_BODY_LIMIT` admite hasta 4096 para versiones cuyo
  contrato se haya verificado; no se actualiza Graph API automáticamente.
- `storing_client` divide overflow sin perder caracteres y guarda cada intento.
  Los títulos/descripciones persistidos coinciden con los normalizados del wire.
- Una respuesta HTTP sin `wamid` válido se registra como `unconfirmed`, no como
  entrega ni fallo definitivo: un timeout puede tener resultado desconocido.
- Si falla un bloque obligatorio previo, no se envían bloques posteriores ni
  controles accionables. Un reintento puede volver a presentar el contenido.

## Telemetría posterior

Mongo conserva las colecciones de historial y agrega:

- `task_runs`: UUID por recorrido, `flow`, subtarea `task`, `release`, `process_id`,
  inicio, final, `outcome`, turnos de entrada, intentos salientes, aceptaciones de
  API y `recovery_turns`. Esta última cuenta turnos con avisos de recuperación,
  incluidos cambios externos, no exclusivamente errores del socio.
- `messages`: correlación `task_run_id`/`turn_id`, paso, versión, subtarea y rol de
  respuesta. El menú final sigue asociado al recorrido aunque la sesión se limpie.
- `message_statuses`: upsert idempotente por `wamid`, con marcas independientes
  sent/delivered/read/failed y precios reportados por Meta. Recibos tardíos o
  anteriores al guardado del mensaje no se pierden ni generan dobles conteos.

`release` toma `RAILWAY_GIT_COMMIT_SHA` o, localmente, `BOT_RELEASE`. Las escrituras
adicionales de ejecuciones/estados son best effort y tienen un máximo de dos
segundos por operación; fallos se registran en logs. No sustituyen la persistencia
de sesión. Recorridos abiertos tras un reinicio pueden quedar `in_progress`: no
contarlos como éxitos ni inventar su resultado; usar proceso, release y timestamps
para identificarlos. Salir explícitamente por menú/ayuda se registra `abandoned`.
Un resultado `success` indica la operación de negocio, no garantiza que el socio
haya recibido el mensaje final.

El contador histórico `conversations.message_count` ya no crece con duplicados
rechazados por `wamid`, pero **no se recalculan ni se sanean los datos antiguos**.
No usar ese contador ni agrupar toda la vida de un teléfono para medir tareas.

### Consulta de tareas completadas (mongosh)

Usar siempre una ventana temporal y una versión explícitas. La mediana se calcula
en mongosh para no exigir `$percentile` de MongoDB 7. Ajustar fechas al despliegue.

```javascript
const release = "REEMPLAZAR_POR_SHA_DEL_DESPLIEGUE";
const from = ISODate("2026-11-07T00:00:00Z");
const to = ISODate("2026-12-01T00:00:00Z");
const median = values => {
  const sorted = values.slice().sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
};
db.task_runs.aggregate([
  {$match: {release, outcome: "success", started_at: {$gte: from, $lt: to}}},
  {$group: {
    _id: {flow: "$flow", task: "$task", release: "$release"},
    completed: {$sum: 1},
    attempts: {$push: "$outbound_attempts"},
    accepted: {$push: "$api_accepted"},
    userTurns: {$push: "$incoming_messages"},
    recoveryTurns: {$sum: "$recovery_turns"}
  }}
]).toArray().map(group => ({
  ...group._id, completed: group.completed,
  medianAttempts: median(group.attempts), medianAccepted: median(group.accepted),
  medianUserTurns: median(group.userTurns), recoveryTurns: group.recoveryTurns
}));
```

Comparar además conteos por todos los outcomes, no solo éxitos. Mantener vistas y
escrituras separadas por `task`; el subtipo final describe el recorrido completo,
incluyendo el catálogo y su menú. Consultas/paginación dentro del recorrido cuentan
en sus totales. No existe un baseline histórico fiable con este nuevo esquema.

### Entregas y mensajes facturables

```javascript
db.messages.aggregate([
  {$match: {direction: "outgoing", release, timestamp: {$gte: from, $lt: to}, wamid: {$type: "string"}}},
  {$lookup: {from: "message_statuses", localField: "wamid", foreignField: "wamid", as: "receipt"}},
  {$unwind: {path: "$receipt", preserveNullAndEmptyArrays: true}},
  {$set: {delivered: {$or: [
    {$eq: [{$type: "$receipt.statuses.delivered"}, "date"]},
    {$eq: [{$type: "$receipt.statuses.read"}, "date"]}
  ]}}},
  {$group: {
    _id: "$task_run_id", accepted: {$sum: 1},
    delivered: {$sum: {$cond: ["$delivered", 1, 0]}},
    billableDelivered: {$sum: {$cond: [{$and: ["$delivered", {$eq: ["$receipt.pricing.billable", true]}]}, 1, 0]}},
    unknownPricingDelivered: {$sum: {$cond: [{$and: ["$delivered", {$ne: [{$type: "$receipt.pricing.billable"}, "bool"]}]}, 1, 0]}}
  }}
]);
```

La ausencia de pricing no significa gratuidad. Un recibo read implica entrega,
pero un mensaje aceptado sin recibos no debe contarse como entregado. Estos
conteos no calculan importes: tarifas, franquicia, mercado y exenciones provienen
de Meta. No multiplicar indiscriminadamente todos los intentos por una tarifa.
Mensajes fuera de una tarea (bienvenida, ayuda o menú en reposo) también llevan
release; en esta consulta quedan agrupados bajo `task_run_id: null`.

**Router upstream:** el código actual reenvía el payload completo, incluidos
estados; los eventos solo de estados no contienen un sender de mensaje y se
dirigen a **prod**. La atribución de recibos a staging requiere ajustar ese
enrutamiento en otro trabajo. Aquí no se modificó el router ni se desplegó nada.

Fuente de facturación efectiva desde octubre de 2026:
https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing#effective-october-1-2026

Los mensajes de servicio entregados son cobrables aun dentro de la ventana de
24 horas, salvo la franquicia mensual de 1000 por número y exenciones aplicables.
Las comparaciones de este cambio prueban menos envíos normales; el ahorro de
factura se valida después con entregas/precios reales, no con medianas inventadas.
