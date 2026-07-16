const { app, BrowserWindow, ipcMain, screen } = require('electron')

let win
let dragInterval = null
let dragOffset = null
let savedBounds = { width: 380, height: 600 }

function createWindow() {
  win = new BrowserWindow({
    width: 380,
    height: 600,
    minWidth: 300,
    minHeight: 460,
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
    win.setSize(70, 80)
    win.setResizable(false)
  } else {
    win.setMinimumSize(270, 460)
    win.setResizable(true)
    win.setSize(savedBounds.width, savedBounds.height)
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