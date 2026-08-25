/** Copies MapLibre's worker bundle into public/ so the map can load it.
 *
 *  Turbopack does not resolve the `new Worker(new URL(..., import.meta.url))`
 *  form inside maplibre-gl's prebuilt ESM: the request 404s, Next serves its
 *  HTML error page, and the browser rejects it for the wrong MIME type. The
 *  map still draws raster tiles on the main thread, so the failure looks like
 *  "the basemap works but no GeoJSON layer ever appears" rather than an
 *  error — which is a long way to debug from the symptom.
 *
 *  Serving the worker as a static asset and pointing `setWorkerUrl` at it
 *  sidesteps the bundler entirely. This runs from predev/prebuild rather than
 *  being committed, so the copy can never drift from the installed version.
 */
import { copyFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const from = join(root, "node_modules", "maplibre-gl", "dist");
const to = join(root, "public", "maplibre");

// The worker imports the shared chunk by relative path, so both have to land
// in the same served directory.
const FILES = ["maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"];

await mkdir(to, { recursive: true });
for (const file of FILES) {
  await copyFile(join(from, file), join(to, file));
}
console.log(`copied ${FILES.length} maplibre worker files to public/maplibre`);
