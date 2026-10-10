// adapter-node (A2 F-02, staging VF-STG-03): `npm run build` emits a
// standalone Node server in build/ (`node build/index.js`), which is what
// web/Dockerfile ships. adapter-auto picked no Node output on a plain
// build host. Runtime env (set by docker-compose.production.yml): HOST,
// PORT, ORIGIN, BODY_SIZE_LIMIT, VIDEO_FACTORY_API_URL.
import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

const config = {
  preprocess: vitePreprocess(),
  kit: {
    // Caddy compresses responses (encode zstd gzip), so no precompressed
    // .gz/.br copies are needed in the image.
    adapter: adapter({ out: 'build', precompress: false })
  }
};

export default config;
