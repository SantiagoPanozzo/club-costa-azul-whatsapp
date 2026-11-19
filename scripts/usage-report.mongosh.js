// Read-only report. Run against the desired environment/database with mongosh.
// Override with: mongosh "$MONGODB_URL" --eval 'var usageDays = 7' scripts/usage-report.mongosh.js
const days = typeof usageDays === "number" && usageDays > 0 && usageDays <= 90 ? usageDays : 30;
const end = new Date();
const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
const coll = db.getCollection("usage_events");
const range = { occurred_at: { $gte: start, $lt: end } };

function report(title, pipeline) {
  print(`\n${title}`);
  printjson(coll.aggregate(pipeline).toArray());
}

print(`Periodo UTC [${start.toISOString()}, ${end.toISOString()}); ${days} días`);
report("Visitas web y navegadores anónimos únicos", [
  { $match: { ...range, channel: "website", event: "site.visit", identification: "anonymous", actor_id: { $type: "string" } } },
  { $group: { _id: "$actor_id", visitas: { $sum: 1 } } },
  { $group: { _id: null, navegadores: { $sum: 1 }, visitas: { $sum: "$visitas" } } },
  { $project: { _id: 0, navegadores: 1, visitas: 1 } },
]);

report("Socios/talleristas web activos", [
  { $match: { ...range, channel: "website", identification: "identified", actor_id: { $type: "string" } } },
  { $group: { _id: "$actor_id" } },
  { $count: "usuarios" },
]);

report("Contactos bot activos por clasificación (pueden solaparse)", [
  { $match: { ...range, channel: "bot", event: "bot.activity", actor_id: { $type: "string" } } },
  { $group: { _id: { identification: "$identification", actor: "$actor_id" } } },
  { $group: { _id: "$_id.identification", contactos: { $sum: 1 } } },
  { $sort: { _id: 1 } },
]);

report("Contactos bot activos en total (deduplicados entre clasificaciones)", [
  { $match: { ...range, channel: "bot", event: "bot.activity", actor_id: { $type: "string" } } },
  { $group: { _id: "$actor_id" } },
  { $count: "contactos" },
]);

report("Usuarios únicos y usos por canal y funcionalidad", [
  { $match: { ...range, channel: { $in: ["bot", "website"] }, identification: "identified", actor_id: { $type: "string" },
    event: { $in: ["activity.view", "activity.enroll", "activity.cancel", "reservation.view", "reservation.create", "reservation.cancel", "fees.view", "receipt.submit", "event.view", "event.enroll", "profile.view", "profile.update", "announcements.view"] } } },
  { $group: { _id: { channel: "$channel", event: "$event", actor: "$actor_id" }, usos: { $sum: 1 } } },
  { $group: { _id: { channel: "$_id.channel", event: "$_id.event" }, usuarios: { $sum: 1 }, usos: { $sum: "$usos" } } },
  { $sort: { "_id.channel": 1, "_id.event": 1 } },
]);

print("Los resultados vacíos significan cero. No sumar visitantes anónimos y miembros ni categorías bot como personas distintas.");
