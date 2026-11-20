# Métricas de uso del bot

El bot guarda eventos best-effort en `usage_events` en MongoDB; no agrega panel ni consulta HTTP. `METRICS_HMAC_KEY` vacío deshabilita las métricas. Usar un secreto distinto por ambiente y del usado por la web; rotarlo cambia los seudónimos. No guardar teléfonos, IDs de mensajes ni contenido en esta colección.

Al arrancar, el bot crea índices de idempotencia, consulta y TTL de 90 días antes de aceptar métricas. Si faltan el secreto o los índices, no registra eventos; si la creación de índices falla, el bot sigue funcionando y reintenta en segundo plano ante actividad, como máximo una vez por minuto. Los eventos durante la falla no se recuperan. El borrado TTL es asíncrono. Las otras colecciones no usan esta retención.

- `bot.activity`: mensaje entrante procesado con `identification` `identified`, `unidentified` o `unknown` (fallo de consulta). Un contacto puede cambiar de categoría durante el período.
- Vistas: `activity.view`, `reservation.view`, `fees.view`, `event.view`, `profile.view`, `announcements.view`. Se cuentan luego de obtener los datos necesarios; una lista vacía también cuenta, salvo el detalle personal no disponible. Indican contenido preparado para responder, no entrega confirmada ni lectura por el socio.
- Acciones exitosas: `activity.enroll`, `activity.cancel`, `reservation.create`, `reservation.cancel`, `receipt.submit`, `event.enroll`. Una reserva creada puede estar pendiente de aprobación y un comprobante pendiente de validación. No se cuenta como éxito una respuesta de error de la API.

El ID del evento deriva mediante HMAC del wamid entrante y el nombre del evento; eso distingue actividad y varios tipos de función en el mismo turno y deduplica reintentos del mismo tipo. Sin wamid se usa un UUID aleatorio, por lo que la deduplicación no está garantizada. Se descartan eventos cuando la cola acotada está llena o falla Mongo; no bloquean el flujo. Son métricas aproximadas, no auditoría de negocio.

## Consultas

`mongosh "$MONGODB_URL" scripts/usage-report.mongosh.js` muestra visitas y navegadores web, miembros web activos, contactos bot por clasificación y en total, y usuarios/usos por función y canal. Por defecto usa los últimos 30 días; para 7 días agregar `--eval 'var usageDays = 7'` antes del archivo. La conexión debe incluir la misma base usada por el bot y la API. El reporte es de solo lectura y usa fechas UTC; períodos fuera de 1 a 90 días vuelven a 30. Las categorías de identificación del bot pueden solaparse; no se suman como personas diferentes. Web y bot no se correlacionan.

## Activación y comprobación en cada ambiente

1. Publicar las tres ramas mediante el proceso de release del proyecto; primero staging. Antes de activar, verificar que la API use `club_costa_azul` y que la base por defecto del URI Mongo del bot apunte a esa misma base del mismo ambiente. No apuntar staging a datos de producción.
2. Configurar secretos `METRICS_HMAC_KEY` distintos para bot y API y distintos por ambiente. En la API, configurar también `METRICS_ALLOWED_ORIGINS` con el origen exacto del frontend. En frontend, `VITE_METRICS_ENABLED=true` debe estar presente **al construir**, junto con `VITE_API_URL`; no es una variable de runtime nginx. No registrar ni compartir los valores de las claves.
3. Probar en staging una visita anónima, un socio y un contacto bot sintéticos o autorizados. Confirmar en Mongo índices TTL y único, documentos de ambos canales en la base esperada y los agregados del reporte. Verificar el límite de 60 requests/minuto por IP percibida por la API detrás del proxy Railway, pues múltiples usuarios podrían compartir esa IP. Probar caída del colector/Mongo sin afectar las tareas.
4. Revisar la información de privacidad aplicable antes de habilitar producción. Validar el comportamiento en staging y recién después habilitarlo en producción mediante el proceso autorizado. No hay backfill y las métricas son aproximadas.
