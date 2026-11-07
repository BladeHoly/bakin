# Bakin — reglas de diseño

Cómo se ve y se comporta la interfaz de Bakin. Valen para la ventana de hoy
(WebView + glass-midas) y para la interfaz nativa de la 1.0: los nombres de
los tokens dicen el tamaño en px para que se lean igual en CSS y en GPUI.

Lo que se puede comprobar solo, se comprueba (al final, «Chequeos»).

## 1. Principios

* **Primero la música y tus cosas.** Las portadas y las fotos son el color de
  la ventana; el resto es vidrio oscuro y texto claro que no compite.
* **Una sola forma de hacer cada cosa.** Si dos pantallas hacen lo mismo,
  se ven y se llaman igual.
* **Calmo.** Nada parpadea, salta ni se mueve sin motivo. Las animaciones
  duran 150–250 ms y se apagan con «Efectos de animación» de Windows.
* **Sin jerga.** «Formato», no «códec»; «calidad», no «bitrate» (salvo en la
  columna técnica que lo muestra).

## 2. Tokens (`desktop/ui/tokens.css`)

### Espacios — cuadrícula de 4 px

| Token | px | Para |
|---|---|---|
| `--sp-2` | 2 | entre un título y su subtítulo |
| `--sp-4` | 4 | dentro de un control (ícono ↔ texto) |
| `--sp-6` | 6 | entre chips de una fila |
| `--sp-8` | 8 | relleno de filas y celdas |
| `--sp-10` | 10 | el hueco entre paneles de la ventana (`--gap`) |
| `--sp-12` | 12 | relleno de tarjetas, entre bloques de una sección |
| `--sp-16` | 16 | relleno de paneles y diálogos |
| `--sp-20` | 20 | entre secciones |
| `--sp-24` | 24 | márgenes de hojas grandes |
| `--sp-32` | 32 | separaciones mayores |

Medidas de diseño mayores a 32 px (ancho de la barra lateral, de una
columna) van como valor propio con nombre (`--side-w`, `--top-h`).

### Letra

Segoe UI Variable (la del sistema). Tamaños:

| Token | px | Para |
|---|---|---|
| `--fs-10` | 10 | insignias diminutas |
| `--fs-11` | 11 | leyendas, contadores |
| `--fs-12` | 12 | ayudas (`.hint`), segunda línea de una fila |
| `--fs-13` | 13 | texto de listas y de Configuración |
| `--fs-14` | 14 | texto principal, botones |
| `--fs-16` | 16 | títulos de sección y de diálogo |
| `--fs-18` / `--fs-22` | 18 / 22 | títulos de vista |
| `--fs-32` | 32 | un número grande (Consumo, estadísticas) |

Peso: 400 el texto, 600 títulos y lo elegido. Números que cambian (tiempos,
contadores) con `font-variant-numeric: tabular-nums`.

### Radios

`--r-4` marcas chicas · `--r-8` miniaturas y campos · `--r-12` mosaicos y
filas · `--r-16` paneles y tarjetas · `--r-24` hojas · `--r-pill` chips y
botones redondos. Lo de adentro tiene un radio menor o igual que su caja.

### Tamaños

* **Nada clickeable mide menos de 24 px** de lado (`--hit-min`, el mínimo de
  WCAG 2.2 AA); lo nuevo apunta a **32 px** (`--hit`). Si el ícono es más chico,
  el área de clic no (relleno, o relleno con margen negativo para no mover la
  línea).
* Íconos: SVG en línea, `viewBox="0 0 24 24"`, trazo 2, extremos redondos,
  `currentColor`, 16 px (`--icon`) en botones y filas.

## 3. Color y vidrio

* Fondo: oscuro y propio de Bakin (`#0d0c14`) con el fondo procedural; el
  único ajuste de apariencia es teñirlo con la portada.
* Texto: `--gm-ink` (claro) y `--gm-ink-muted` (secundario). Nunca gris sobre
  gris: contraste ≥ 4,5:1 para texto chico.
* Superficies: blanco con transparencia sobre el fondo: `--c-fill-1/2/3`
  (0,04 / 0,06 / 0,1), líneas `--c-line` (0,15) y `--c-line-strong`, texto
  tenue `--c-ink-faint/dim/soft`; sombras `--c-shade-1…4`. Ningún color suelto
  en el CSS: todos son tokens de `tokens.css` (los de glass-midas, `--gm-*`). Los estados (éxito, aviso, error,
  info) usan los colores de glass-midas (`--gm-status-*`) y siempre llevan
  texto o ícono además del color.
* **Vidrio** (glass-midas): niveles `lite` (la mayoría), `glass` (paneles
  que flotan) y `blur` solo donde hace falta leer sobre contenido que se mueve
  (la barra de arriba). El desenfoque cuesta memoria y GPU: no se agrega «por
  lindo».
* glass-midas no se edita en `vendor/`: si falta algo, se pide
  (`docs/glass-midas-component-requests.md`) y se actualiza desde npm.

## 4. Ventana y anchos

* Mínimo de la ventana: 520 × 560. Se prueba a **1280, 980, 720** de ancho y
  en un monitor vertical.
* Cortes (`app.css`):
  * **1100**: la barra de arriba se compacta (sin el atajo en la búsqueda)
    y la lista de música oculta Álbum;
  * **1000**: «Descargar» queda solo con su ícono;
  * **900**: la barra lateral se pliega detrás del ☰, el panel Descargar
    pone su menú arriba y los presets van en una columna;
  * **760**: las estadísticas en una columna;
  * **640**: la lista de música queda con Título y Duración;
  * **560**: «En tu casa» apila el QR y los datos.
* Nada provoca desplazamiento horizontal de la ventana. Un texto largo se
  corta con «…» y muestra el texto completo en `title`.
* Las listas largas se virtualizan; el desplazamiento nunca salta al cargar.

## 5. Estados

Cada lista o panel tiene sus tres estados además del normal:

* **Vacío:** una frase que dice qué va ahí y cómo llenarlo («Para seguir un
  canal, pega el enlace…»), sin ilustraciones grandes.
* **Cargando:** el esqueleto de la lista o un indicador en el lugar; nunca
  bloquear toda la ventana. Si tarda más de 2 s, decir qué está haciendo.
* **Error:** qué pasó y qué hacer, en una línea («No se pudo conectar con
  Wallhaven. Reintentar»), con el detalle técnico solo en el registro.

Avisos (toasts): uno a la vez, abajo, 5–9 s según el largo; con «Deshacer»
cuando se puede. Nunca tapan un diálogo.

## 6. Texto

* Español neutro con «tú» (la interfaz); el español del código es la clave
  de traducción (`tr()`), el inglés está en `desktop/ui/i18n/en.js`.
* Botones con verbo («Descargar», «Agregar una carpeta…»); «…» cuando abre
  algo más antes de actuar.
* Fechas y números con el idioma elegido (`LOCALE`).
* Nada de mayúsculas sostenidas salvo los rótulos de sección chicos.

## 7. Teclado y accesibilidad

* Todo se puede hacer con el teclado; el foco se ve (anillo de 2 px).
* Los botones solo-ícono tienen `aria-label` y `title`.
* Roles ARIA solo cuando el elemento nativo no alcanza; no se inventan
  listas o grillas que no se comportan como tales.

## 8. Chequeos

* `packaging/css_lint.py` (y `tests/test_design.py`): espacios, radios y
  letra de `app.css` y `mini.css` solo con tokens.
* Smoke (`packaging/ui-smoke.mjs`): axe sin fallas graves (C103), textos que
  entran, sin desbordes en los anchos de arriba (0.23.2).
* Capturas antes/después de cada cambio visual, con imágenes de muestra.
