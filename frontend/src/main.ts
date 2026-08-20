import { createApp } from "vue";

import App from "./App.vue";
import { frontendAppName } from "./branding";
import { queryClient } from "./queryClient";
import { router } from "./router";
import "./styles.css";
import { VueQueryPlugin } from "@tanstack/vue-query";

document.title = frontendAppName;
createApp(App).use(router).use(VueQueryPlugin, { queryClient }).mount("#app");
