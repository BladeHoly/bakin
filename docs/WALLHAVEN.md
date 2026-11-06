# Fondos de Wallhaven — investigación y propuesta

Investigado el 2026-11-02 con la documentación oficial
(https://wallhaven.cc/help/api), los términos (https://wallhaven.cc/terms)
y tres pedidos reales a la API (sin clave, solo SFW).

## Cómo funciona Wallhaven

* **API pública y gratuita**, en `https://wallhaven.cc/api/v1`. Devuelve JSON.
  «Provided for free and as-is»: puede cambiar sin aviso.
* **Sin clave** se puede buscar y bajar todo lo **SFW** y lo «sketchy».
  Probado: pedir NSFW sin clave no da error, simplemente no lo incluye.
* **Con clave** (cada usuario la saca de su cuenta en wallhaven.cc): NSFW,
  colecciones privadas y los filtros guardados del usuario. Se manda en el
  encabezado `X-API-Key` (mejor que en la URL, así no queda en logs).
* **Límite: 45 pedidos por minuto**; si se pasa, responde **429**.
* **24 resultados por página**; `meta` trae `current_page`, `last_page`,
  `total` y, con orden al azar, una `seed` para no repetir entre páginas.

### Lo que se puede pedir

| Pedido | Para qué |
|---|---|
| `GET /search` | Buscar. `q` (tags: `gato`, `-tag`, `+tag1 +tag2`, `@usuario`, `like:ID`, `type:png`), `categories` (general / anime / personas), `purity` (sfw / sketchy / nsfw), `sorting` (`date_added`, `relevance`, `random`, `views`, `favorites`, `toplist`), `topRange` (1d … 1y, con `toplist`), `atleast` (resolución mínima), `resolutions`, `ratios` (**acepta varias**, probado `16x9,21x9`), `colors`, `page`, `seed` |
| `GET /w/<id>` | Un fondo con todo: `path` (archivo completo, `https://w.wallhaven.cc/full/…`), `thumbs` (`small`, `large`, `original`), `resolution`, `ratio`, `file_size`, `file_type`, `colors`, `source`, `uploader` y **`tags`** (la búsqueda no trae tags ni autor) |
| `GET /tag/<id>` | Datos de un tag |
| `GET /collections/<usuario>` y `/collections/<usuario>/<id>` | Colecciones públicas de alguien y sus fondos |
| `GET /collections` y `/settings` | Las del propio usuario (también privadas) y sus filtros — **con clave** |

### Reglas

* Los términos (de 2016) no dicen nada sobre acceso automático, descargas ni
  la API: no lo prohíben ni lo permiten explícitamente.
* «All images remain property of their original owners»: uso personal, sin
  redistribuir. Bakin no trae ni publica fondos; cada usuario baja los suyos.
* gallery-dl (que Bakin ya incluye) sabe bajar de Wallhaven, pero usa una
  **clave compartida que trae de fábrica**. No corresponde usar la clave de
  otro: Bakin tendrá su propio cliente, sin clave por defecto.

## Propuesta

### Fase 1 — Buscar y bajar (lo mínimo útil) — ✓ publicada en la 0.10.0 (2026-11-02)

Implementada en `bakin/wallhaven.py` (cliente, sin clave), `service.py` (trabajo «wallhaven» en la
cola, `wallhaven_search` / `wallhaven_download`) y el diálogo «Fondos de Wallhaven» en la
interfaz. Probada contra la API real: dos fondos verticales bajados con sus tags. Tests:
`tests/test_wallhaven.py` (sin red) y ui-smoke C40.


* **«Descargar ▾» › «Fondos de Wallhaven…»**: un diálogo con buscador por
  tags, categorías (General · Anime · Personas), orden (Más populares del
  mes por defecto, Recientes, Al azar, Más vistos, Favoritos) y colores.
* **Pensado para tus pantallas**: Bakin detecta tus monitores y propone
  resolución mínima y proporción de cada uno (por ejemplo 16:9 para el
  principal y 9:16 para el vertical). Un selector «Para: monitor 1 / monitor
  2 / cualquiera».
* **Vista previa antes de bajar**: grilla con las miniaturas (`thumbs.large`)
  y la resolución; elegís cuáles, o «Bajar los N» con un límite.
* **Pegar enlaces de Wallhaven** en la caja de enlaces: un fondo
  (`wallhaven.cc/w/…`, `whvn.cc/…`), una búsqueda o una colección pública.
* **Los tags de Wallhaven pasan a ser tags de Bakin**, así la biblioteca ya
  queda organizada y se busca por tag (el sistema de tags existe).
* **Sin repetidos**: un registro de lo ya bajado (por id de Wallhaven).
* **Respetuoso con el límite**: el cliente no pasa de ~40 pedidos por minuto
  (uno cada 1,5 s), las imágenes se bajan de a una, y ante un 429 se para y
  espera (igual que con YouTube). Contenido **SFW por defecto**, «sketchy»
  como opción; NSFW nunca.
* Carpeta propia, por ejemplo `Descargas\Wallhaven\<búsqueda o colección>`.

### Fase 2 — Tu cuenta y tus colecciones

* **Clave opcional** (Configuración › Wallhaven): habilita tus colecciones
  (también privadas) y tus filtros. Guardada en el Administrador de
  credenciales de Windows, nunca en archivos ni logs (como las sesiones hoy).
* **Seguir una búsqueda o una colección**: Bakin revisa cada tanto y baja lo
  nuevo (reusa «Seguir», que ya existe).

### Fase 3 — Que sean tus fondos de verdad

* **Poner como fondo de Windows**, **por monitor** (Windows lo permite con
  `IDesktopWallpaper`): clic derecho › «Poner de fondo en el monitor 2».
* **Rotación**: cambiar el fondo cada X tiempo desde una colección, una
  búsqueda o tus favoritas, por monitor. Liviano: lo hace el shell (Rust),
  sin abrir la ventana.
* Vista «Fondos» en Fotos: por resolución, proporción y color.

## Decisiones (2026-11-02)

1. **Sin NSFW**, nunca. **«Sketchy» sí**, como opción que se prende a
   propósito (apagada por defecto). Así no hace falta clave en la Fase 1.
2. La pestaña **sigue siendo «Fotos»**: los fondos son fotos de la
   biblioteca, como las demás.
3. Se empieza por la **Fase 1**.
