const { app, BrowserWindow, ipcMain, screen } = require('electron')

const MIN_WIDTH = 300
const MIN_HEIGHT = 460

let win
let dragInterval = null
let dragOffset = null
let collapsed = false
let ignoringMouse = false

function createWindow() {
  win = new BrowserWindow({
    width: 380,
    height: 600,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    frame: false,
    alwaysOnTop: true,
    transparent: true,
    resizable: true,
    hasShadow: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    }
  })
  win.loadFile('index.html')
}

function setIgnore(ignore) {
  ignoringMouse = !!ignore
  if (ignore) win.setIgnoreMouseEvents(true, { forward: true })
  else win.setIgnoreMouseEvents(false)
}

// Collapsed = click-through window with the orb still in place (no resize flash).
ipcMain.on('collapse', (event, isCollapsed) => {
  collapsed = !!isCollapsed
  if (collapsed) setIgnore(true)
  else setIgnore(false)
  event.returnValue = true
})

ipcMain.on('set-ignore-mouse', (event, ignore) => {
  if (!collapsed) {
    event.returnValue = true
    return
  }
  setIgnore(!!ignore)
  event.returnValue = true
})

ipcMain.on('drag-start', (event, { screenX, screenY }) => {
  const b = win.getBounds()
  dragOffset = { x: screenX - b.x, y: screenY - b.y }
  if (dragInterval) clearInterval(dragInterval)
  dragInterval = setInterval(() => {
    const c = screen.getCursorScreenPoint()
    win.setPosition(Math.round(c.x - dragOffset.x), Math.round(c.y - dragOffset.y))
  }, 8)
})

ipcMain.on('drag-stop', () => {
  if (dragInterval) { clearInterval(dragInterval); dragInterval = null }
})

app.whenReady().then(createWindow)
