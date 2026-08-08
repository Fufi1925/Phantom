    prefix = s.root_path  # e.g. "/phantom"
    if prefix:
        outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        
        # --- HIER ZWISCHEN EINBAUEN ---
        @outer.get("/admin-panel")
        async def owner_admin_panel(request: Request):
            user = request.session.get("user")
            # Falls "id" ein Integer ist, wandeln wir es zum Vergleich in String um
            if not user or str(user.get("id")) != "1523728380476919910":
                raise HTTPException(status_code=403, detail="Zugriff verweigert")
            return HTMLResponse("<h1>Willkommen im Admin Panel, Chef!</h1>")
        # ------------------------------
        
        outer.mount(prefix, inner)
        
        @outer.get("/")
        async def _root():
            return RedirectResponse(url=prefix + "/login")
        return outer
