# Editor de Video con IA

Sube un video, escribe qué quieres hacer, y descarga el resultado.

## Instalación (una sola vez)

```bash
pip install -r requirements.txt
```

## Correr la app

```bash
uvicorn main:app --reload
```

Luego abre tu navegador en: **http://localhost:8000**

## Prompts de ejemplo

| Lo que escribes | Lo que hace |
|---|---|
| `Recorta del segundo 5 al 20` | Corta esa parte del video |
| `Agrega el texto 'Mi Empresa' arriba` | Pone texto en la parte superior |
| `Agrega el texto 'Promo' abajo` | Pone texto en la parte inferior |
| `Doble velocidad` | Duplica la velocidad |
| `Mitad de velocidad` | Pone el video a cámara lenta |
| `Quita el audio` | Elimina el sonido |
| `Cambia a 720p` | Reduce la resolución a 720p |
| `Recorta del segundo 0 al 10 y agrega texto 'Hola' arriba` | Combina varias ediciones |
