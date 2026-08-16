# public/

Files here are copied to the root of the build with their names intact — every
other asset gets a content hash, so this is the only place a stable URL exists.

## logo.png

Drop the company logo here as `logo.png`. It is used in three places: the
sidebar next to the name, inside the circle on the sign-in screen, and as the
browser tab icon.

**512×512, PNG, transparent background, square canvas.** Every use is small
(20–40px on screen), but 512 covers retina displays and anything added later
without regenerating variants — browsers downscale cleanly and upscale badly.

A non-square logo still works: it is drawn with `object-fit: contain`, so it
fits without distortion and simply leaves space at the sides.

If this file is absent, the app falls back to its built-in mark rather than
showing a broken image.
