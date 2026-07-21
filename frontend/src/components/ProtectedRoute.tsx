import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { Spin, message } from 'antd';
import { authApi } from '../services/api';
import { sessionManager } from '../utils/sessionManager';

interface ProtectedRouteProps {
  children: ReactNode;
}

export default function ProtectedRoute({ children }: ProtectedRouteProps) {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null);
  const location = useLocation();

  useEffect(() => {
    const checkAuth = async () => {
      try {
        await authApi.getCurrentUser();
        setIsAuthenticated(true);
        sessionManager.setWarningCallback((msg) => {
          message.warning({ content: msg, duration: 10 });
        });
        sessionManager.start();
      } catch {
        setIsAuthenticated(false);
        sessionManager.stop();
      }
    };
    checkAuth();
    
    return () => {
    };
  }, []);

  if (isAuthenticated === null) {
    return (
      <div style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '100vh',
      }}>
        <Spin size="large" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to={`/login?redirect=${encodeURIComponent(location.pathname)}`} replace />;
  }

  return <>{children}</>;
}