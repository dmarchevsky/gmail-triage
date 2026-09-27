import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { applyOpenParam } from "./deeplink";
import { registerServiceWorker } from "./push";
import "./styles.css";

applyOpenParam(); // before the HashRouter reads the URL
registerServiceWorker();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
