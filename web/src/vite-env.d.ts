/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** build id of this bundle (see vite.config.ts); "" outside an image build */
  readonly VITE_BUILD_ID: string;
}
