# Métricas de uso del bot

El bot guarda eventos best-effort en `usage_events` en MongoDB; no agrega panel ni consulta HTTP. `METRICS_HMAC_KEY` vacío deshabilita las métricas. Usar un secreto distinto por ambiente y del usado por la web; rotarlo cambia los seudónimos. No guardar teléfonos, IDs de mensajes ni contenido en esta colección.

Al arrancar, el bot crea índices de idempotencia, consulta y TTL de 90 días antes de aceptar métricas. Si faltan el secreto o los índices, no registra eventos; si la creación de índices falla, el bot sigue funcionando pero las métricas permanecen desactivadas hasta reiniciarlo. El borrado TTL es asíncrono. Las otras colecciones no usan esta retención.

- `bot.activity`: mensaje entrante procesado con `identification` `identified`, `unidentified` o `unknown` (fallo de consulta). Un contacto puede cambiar de categoría durante el período.
- Vistas: `activity.view`, `reservation.view`, `fees.view`, `event.view`, `profile.view`, `announcements.view`. Se cuentan luego de obtener los datos necesarios; una lista vacía también cuenta, salvo el detalle personal no disponible. Indican contenido preparado para responder, no entrega confirmada ni lectura por el socio.
- Acciones exitosas: `activity.enroll`, `activity.cancel`, `reservation.create`, `reservation.cancel`, `receipt.submit`, `event.enroll`. Una reserva creada puede estar pendiente de aprobación y un comprobante pendiente de validación. No se cuenta como éxito una respuesta de error de la API.

El ID del evento deriva mediante HMAC del wamid entrante y el nombre del evento; eso distingue actividad y varios tipos de función en el mismo turno y deduplica reintentos del mismo tipo. Sin wamid se usa un UUID aleatorio, por lo que la deduplicación no está garantizada. Se descartan eventos cuando la cola acotada está llena o falla Mongo; no bloquean el flujo. Son métricas aproximadas, no auditoría de negocio.
