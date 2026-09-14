import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { getInitialLanguage, LanguageProvider } from './i18n/LanguageContext';
import { languageCatalogs } from './i18n/languageCatalogs';
import { AuthProvider } from './store/authStore';
import './styles/responsive-scale.css';
import './styles/global.css';
import './styles/reference-v8.css';
import './styles/workflow-guide.css';
import './styles/motion.css';
import './styles/motion-polish.css';

const root = ReactDOM.createRoot(document.getElementById('root') as HTMLElement);
const initialLanguage = getInitialLanguage();

function renderApplication() {
  root.render(
    <React.StrictMode>
      <BrowserRouter useTransitions>
        <LanguageProvider initialLanguage={initialLanguage}>
          <AuthProvider>
            <App />
          </AuthProvider>
        </LanguageProvider>
      </BrowserRouter>
    </React.StrictMode>,
  );
}

if (languageCatalogs.isReady(initialLanguage)) {
  renderApplication();
} else {
  root.render(
    <main className="route-loading" role="status" aria-busy="true" lang={initialLanguage}>
      <span className="route-loading-track" aria-hidden="true" />
      <span className="route-loading-label">
        {initialLanguage === 'de' ? 'Wird geladen' : '読み込み中'}
      </span>
    </main>,
  );
  void languageCatalogs.load(initialLanguage).catch((error: unknown) => {
    // Preserve the existing English fallback if a locale chunk is unavailable.
    console.warn('Unable to load the initial language.', error);
  }).then(renderApplication);
}
