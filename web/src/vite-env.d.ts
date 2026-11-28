/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** build id of this bundle (see vite.config.ts); "" outside an image build */
  readonly VITE_BUILD_ID: string;
  /** the TV app's proposed server address (tv-app build) */
  readonly VITE_TV_APP_BASE?: string;
}
