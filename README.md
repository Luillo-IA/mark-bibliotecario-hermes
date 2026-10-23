# MARK Biblioteca para Hermes

Consulta y renovación automática de préstamos del Portal del Lector de la
Comunidad de Madrid, con varias cuentas, estado local y resumen en Telegram.

MARK tiene dos caminos separados:

- Una rutina de Python consulta el portal y renueva los préstamos elegibles
  **cada día a las 09:00, Europe/Madrid**, sin llamar a un modelo de IA.
- El perfil de conversación responde sobre títulos y fechas leyendo la última
  tabla guardada, sin abrir el navegador por una pregunta del chat.

El **domingo a las 09:00** la rutina renueva primero y envía el resumen de libros
y devoluciones. El resto de días guarda silencio en Telegram. Las respuestas a
preguntas sí utilizan el modelo que ya tengas configurado en Hermes.

## Qué incluye

- Varias cuentas de biblioteca, con un identificador interno por lector.
- Estado e historial mínimo en SQLite, con última fecha de consulta.
- Confirmación de renovación solo si se observa una fecha de devolución posterior.
- Bloqueo entre operaciones y registro previo a cada intento: una operación
  incierta no se vuelve a pulsar a ciegas.
- Conservación del último listado correcto cuando falla una consulta.
- Lectura de reservas existentes cuando se reconoce el formato; **no crea reservas**.
- Plugin, skill, instrucciones, proceso de navegador separado y dos tareas del
  programador nativo de Hermes. No necesita otro bot ni otro servicio permanente.

Este es un proyecto comunitario, sin afiliación con la Comunidad de Madrid,
Telegram o Hermes. Se dirige al Portal del Lector, no a cualquier biblioteca.

## Entorno compatible

El instalador está orientado a **Ubuntu 24.04, Python 3.12 y Hermes Agent 0.22.2**
nativo, con un usuario de servicio `hermes`, un gateway de systemd y Telegram ya
configurados. No instala Hermes ni concede sudo a su usuario.

Por defecto espera el hogar de servicio en `/var/lib/hermes`, el estado en
`/var/lib/hermes/.hermes` y el gateway `hermes-gateway.service`. Una instalación
de Hermes bajo tu usuario habitual requiere adaptar el despliegue: este
instalador no se presenta como universal ni como compatible con Windows.

El proveedor de modelos debe funcionar ya mediante la configuración y los
almacenes compartidos nativos de Hermes. No se requieren suscripciones ni
complementos OAuth concretos. La voz bajo petición depende del TTS de Hermes
que tengas instalado; no se cambia su motor.

## Preparación

Clona este repositorio en tu servidor:

```bash
git clone https://github.com/Luillo-IA/mark-bibliotecario-hermes.git
cd mark-bibliotecario-hermes
```

Para ejecutar el instalador, su intérprete
necesita PyYAML; en Ubuntu puede instalarse mediante `python3-yaml`.

Crea un grupo privado de Telegram llamado **MARK** con tu bot existente y las
personas que vayan a compartir la biblioteca. Envía un mensaje al grupo. El bot
debe poder leer los mensajes: el instalador comprueba los metadatos y el acceso.

Prepara las cuentas sin pegarlas en la conversación:

```bash
mkdir -m 700 .private
cp examples/credenciales.example.json .private/credenciales.json
chmod 600 .private/credenciales.json
```

Edita el archivo local con tu editor y sustituye todos los valores de ejemplo.

| Campo | Uso |
|---|---|
| `id` | Etiqueta interna estable y única, por ejemplo `lector1`. |
| `nombre` | Nombre que aparecerá en el resumen. |
| `carnet` | Carné de lector del portal. |
| `password` | Contraseña del portal. |

Las credenciales se guardan como JSON local protegido por permisos, **no cifrado**.
No deben entrar en Git ni en mensajes de Telegram. Tampoco deben publicarse la
base de datos, los informes operativos, sesiones o respaldos con datos reales.

## Instalar

Desde la raíz del repositorio:

```bash
sudo python3 install.py --group-id=-1000000000000
```

Sustituye el ID ficticio por el de tu grupo. También puede omitirse `--group-id`
si Hermes conoce un único grupo llamado MARK y puede identificarlo por sus
metadatos. El instalador verifica que el grupo corresponda a ese nombre.

El instalador prepara el perfil `mark`, instala Playwright y Chromium en un venv
independiente y añade las bibliotecas del sistema que falten. Después consulta
las cuentas sin renovar y comprueba el modelo usando solo la tabla local.
Esta prueba puntual usa tu proveedor actual y puede consumir su cuota.

Solo al superar las comprobaciones activa la ruta y las tareas. La instalación
inicial reinicia de forma controlada el gateway existente y comprueba su vuelta.
Conserva las rutas previas y guarda respaldo privado antes de cambiar configuración.
Lee `install.py` antes de ejecutarlo con sudo y comprueba que tu entorno coincide.

Opciones útiles:

```bash
sudo python3 install.py --prepare                 # Perfil aparcado, sin cuentas reales ni activación
sudo python3 install.py --test-renewal            # Incluye intentos REALES de renovación elegible
sudo python3 install.py --apply-accounts          # Valida y aplica cuentas editadas, sin reiniciar
sudo python3 install.py --daily-review            # Actualiza un despliegue antiguo a frecuencia diaria
sudo python3 install.py --diagnose-last-failure   # Diagnóstico por categorías, sin exportar logs privados
```

No reinstales sobre un perfil activo. Los informes de instalación van a `reports/`
y los respaldos privados al directorio `migration/` del hogar del servicio.
Los respaldos de una instalación pueden contener credenciales y deben conservar
sus permisos. No revierten una renovación ya aceptada por la biblioteca.

Para otras rutas de servicio se admiten `MARK_ACCOUNT_HOME`, `MARK_ACCOUNTS_FILE`,
`MARK_REPORT_DIR` y `MARK_GATEWAY_SERVICE`. Al usar sudo pásalas explícitamente,
por ejemplo `sudo env MARK_ACCOUNTS_FILE=/ruta/privada/cuentas.json python3 install.py`.
El usuario Linux del servicio sigue siendo `hermes`.

## Uso y programación

Pregunta en el grupo: «¿Qué libros tenemos y cuándo hay que devolverlos?».
La respuesta debe mostrar los datos guardados y cuándo se actualizaron. Una
pregunta no dispara consultas web ni renovaciones. El perfil tiene herramientas
limitadas a biblioteca local, memoria y síntesis bajo petición si está disponible.

Las tareas nativas, sin agente, son:

| Tarea | Cron local | Efecto |
|---|---|---|
| Revisión silenciosa | `0 9 * * 1-6` | Consulta, renueva y guarda; lunes a sábado. |
| Domingo | `0 9 * * 0` | Consulta, renueva, guarda y después envía el resumen. |

Así hay una revisión diaria y una sola tarea de navegador cada día. El horario
usa `Europe/Madrid` y contempla sus cambios de hora. Los avisos y entregas nativos
de cron quedan locales; el envío de Telegram se controla en el código dominical.
Una entrega incierta no se reintenta automáticamente ni se envía otro día.

## Límites conocidos

- El portal puede cambiar su HTML. Una extracción incompleta se registra como
  fallo y no como cero préstamos. Los casos ambiguos se rechazan.
- Una reserva ajena, sanción, límite o plazo de renovación pueden impedir renovar.
  Se reconocen determinados textos visibles; no todas las variantes de mensajes
  o ventanas emergentes están cubiertas.
- El motivo de no renovación se guarda cuando se reconoce, pero el resumen
  dominical todavía no explica cada motivo junto a cada libro.
- Las reservas no reconocidas se indican como no comprobadas, no como inexistentes.
- Las pruebas usan cuentas y HTML ficticios. La primera renovación elegible y
  entrega dominical de cada instalación necesitan su propia verificación real.

## Pruebas

No requieren cuentas reales, tokens ni contacto con el portal:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium --only-shell
.venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
```

Chromium necesita sus bibliotecas del sistema. Las pruebas de interfaces nativas
se omiten si no se define `HERMES_SOURCE` con un checkout de Hermes compatible.
Ejecuta primero las pruebas del propio Hermes para preparar sus dependencias,
y usa `HERMES_TEST_DEPS` / `HERMES_EXTRA_TEST_DEPS` si están en directorios separados.

## Procedencia y mantenimiento

El flujo se inspiró en BiblioBotMadrid. Esta adaptación separa navegador, estado,
programador y conversación de Hermes. No incluye los archivos originales de ese
proyecto, credenciales ni datos de ninguna instalación particular.

Trabaja en Git por cambios terminados y comprobados: crea un commit al cerrar
cada cambio relevante y publica los commits autorizados. Un commit local no es
una copia fuera del servidor hasta que se sube al remoto. La base de datos y
credenciales requieren un respaldo privado independiente de este repositorio.

## Licencia

El código de esta adaptación se distribuye bajo la [licencia MIT](LICENSE).
No incluye ni relicencia el proyecto original que inspiró su funcionamiento.
