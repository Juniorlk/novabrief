import { createApp } from "vue";

import App from "./App.vue";
import { createAppI18n } from "./i18n";

createApp(App).use(createAppI18n()).mount("#app");
