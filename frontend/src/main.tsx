import ReactDOM from 'react-dom/client';
import { QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { AppRouter } from './app/router';
import { queryClient } from './shared/api/query';
import './style.css';

ReactDOM.createRoot(document.getElementById('root')!).render(<QueryClientProvider client={queryClient}><AppRouter mode="web"><App/></AppRouter></QueryClientProvider>);
