import { createApp } from "vue";

import App from "./App.vue";
import { frontendAppName } from "./branding";
import { queryClient } from "./queryClient";
import { router } from "./router";
import "./styles.css";
import { VueQueryPlugin } from "@tanstack/vue-query";
import { i18n } from "./i18n";

document.title = frontendAppName;
createApp(App).use(router).use(i18n).use(VueQueryPlugin, { queryClient }).mount("#app");
