const { app, BrowserWindow, ipcMain, screen } = require('electron')

const MIN_WIDTH = 300
const MIN_HEIGHT = 460
const COLLAPSED_SIZE = { width: 70, height: 80 }

let win
let dragInterval = null
let dragOffset = null
let savedBounds = { width: 380, height: 600 }

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
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    }
  })
  win.loadFile('index.html')
}

ipcMain.on('collapse', (event, isCollapsed) => {
  if (isCollapsed) {
    const bounds = win.getBounds()
    savedBounds = { width: bounds.width, height: bounds.height }
    win.setMinimumSize(50, 50)
    win.setSize(COLLAPSED_SIZE.width, COLLAPSED_SIZE.height)
    win.setResizable(false)
  } else {
    win.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
    win.setResizable(true)
    win.setSize(
      Math.max(savedBounds.width, MIN_WIDTH),
      Math.max(savedBounds.height, MIN_HEIGHT)
    )
  }
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