# How a push-broom (line-scan) hyperspectral camera forms an image

The Specim FX10e has an ordinary 2D sensor, but in front of it sit a narrow **slit** and a **spectrograph**.

- The slit admits light from one thin **line** of the scene. With the 38° lens at 480 mm distance that line is
  about 330 mm long and 0.3 mm wide.
- The spectrograph spreads the light of that line by **wavelength** along the other sensor axis.
- So one camera *frame* is not a picture. It is a 2D array whose one axis is **position along the line**
  (1024 spatial pixels) and whose other axis is **wavelength** (224 bands after binning the 448 sensor rows by two).
  Every one of the 1024 points on the line gets a full spectrum in every frame.

The 38° field of view is real, but only across the track, along that line. Along the track the field of view is the
slit width, essentially one pixel. The camera sees a wide line, not a wide area.

To get a spatial image you **move the line over the scene**; every frame becomes one row of the final image:

- frames per second × seconds = number of rows
- carrier speed ÷ frame rate = row spacing (along-track pixel size)
- with 0.3 mm sampling across the track, square pixels need the same spacing along it: at 163 fps (the camera's
  maximum at the default region) that is at most about 5 cm/s; at 10 cm/s and 50 fps the rows are 2 mm apart.

The result is a **data cube**: rows along the track × 1024 columns across it × 224 bands deep, stored here as ENVI
band-interleaved-by-line (BIL). The carrier is not an add-on: it is how the camera forms an image at all. A fixed
camera over a moving stage, or a moving camera over a fixed scene, are the two usual setups.

Numbers for this camera: sensor 1312 × 1082; usable spatial region 1024 pixels (the edges are not illuminated evenly
by the optics); 12-bit output.

Practical consequences

- Focus and exposure are hard to judge from a single line. The service preview stitches the most recent few hundred
  frames into a false-colour strip; moving anything under the camera makes it show an image.
- The carrier must move **perpendicular to the slit** so the 1024-pixel line sweeps sideways. If the slit is aligned
  with the direction of travel, the same line is scanned over itself. Check the orientation once, with the preview,
  before the first real scan.
- Acceleration ramps of the carrier bunch the rows up; crop by the recorded positions. The camera has a trigger
  input, but this service runs it free-running and offers no trigger setting.
- Reflectance needs a dark reference (shutter closed) and a white reference (a calibration tile) per scan.
