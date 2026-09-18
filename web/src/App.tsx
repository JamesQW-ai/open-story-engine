import { useEffect, useState } from 'react'
import {
  BrowserRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom'
import { PackagesPage } from './pages/PackagesPage'
import { PackageDetailPage } from './pages/PackageDetailPage'
import { SessionsPage } from './pages/SessionsPage'
import { SessionPage } from './pages/SessionPage'

function Shell() {
  const location = useLocation()
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    try {
      return localStorage.getItem('story-theme') === 'light' ? 'light' : 'dark'
    } catch {
      return 'dark'
    }
  })
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem('story-theme', theme)
    } catch {
      /* 私密模式仍可切换本次主题 */
    }
  }, [theme])
  useEffect(() => {
    if (!(location.state as { preserveScroll?: boolean } | null)?.preserveScroll) window.scrollTo(0, 0)
  }, [location.pathname])
  return (
    <div className="app-shell">
      <header className="app-header">
        <NavLink to="/packages" className="brand">
          <span className="brand-mark">阅</span>
          <span>未完之书</span>
        </NavLink>
        <nav aria-label="主导航">
          <NavLink to="/packages">书架</NavLink>
          <NavLink to="/sessions">我的故事</NavLink>
        </nav>
        <div className="header-actions">
          <button
            className="theme-toggle"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            aria-label={`切换到${theme === 'dark' ? '浅' : '深'}色主题`}
            title={`切换到${theme === 'dark' ? '浅' : '深'}色主题`}
          >
            <span aria-hidden="true">{theme === 'dark' ? '☼' : '☾'}</span>
            <span className="theme-label">
              {theme === 'dark' ? '浅色' : '深色'}
            </span>
          </button>
        </div>
      </header>
      <Routes>
        <Route path="/" element={<Navigate to="/packages" replace />} />
        <Route path="/packages" element={<PackagesPage />} />
        <Route
          path="/packages/:packageId/:version"
          element={<PackageDetailPage key={location.pathname} />}
        />
        <Route path="/sessions" element={<SessionsPage />} />
        <Route
          path="/sessions/:sessionId"
          element={<SessionPage key={(location.state as { openingKey?: string } | null)?.openingKey ?? location.pathname} />}
        />
        <Route path="*" element={<Navigate to="/packages" replace />} />
      </Routes>
    </div>
  )
}
export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  )
}
