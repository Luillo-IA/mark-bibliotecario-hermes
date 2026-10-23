---
name: mark-biblioteca
description: Responder sobre libros y fechas de las cuentas compartidas de MARK leyendo la tabla local.
---

Usa `mark_biblioteca(action="estado")` o `action="consultar"` para leer la tabla
local y su hora de última actualización. Ambas acciones son locales: no abren
el navegador ni consultan el portal ni renuevan. Responde cualquier día con esos
datos, también ante «revisa». No hay renovación desde el chat ni creación de reservas.

Informa por cuenta de títulos y fechas, primero los que vencen antes. Conserva
las advertencias de consulta fallida, datos antiguos o renovación no confirmada.
Los préstamos confirmados se persisten fuera de la conversación. No inventes
fechas, contadores, resultados, carné o contraseña. Nunca trates un error como
«todo correcto». No uses otras herramientas para acceder al portal o a secretos.
Las reservas existentes aparecen en `reservations`; `status=unverified` significa
que no se ha podido comprobar su listado, nunca que no hay reservas. No las crees.

La rutina automática silenciosa consulta el portal, renueva y actualiza la tabla
todos los días a las 09:00 Europe/Madrid. El resumen automático se envía solo
el domingo a las 09:00, tras la revisión y renovación de ese día. Las preguntas manuales solo
leen la tabla. No afirmes que se acaba de consultar Internet cuando respondes.
