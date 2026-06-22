I can't write files directly in Plan mode, but I can output the full markdown content for you to save. Here it is:

````markdown
# Android Version — Radio Tracker App

**Status**: Framework / Planning Document  
**Date**: 2026-06-20

---

## Architecture Overview

```
┌─────────────────────┐      HTTPS/JSON + JWT       ┌──────────────────────┐
│  Android App        │◄────────────────────────────►│  Django Backend      │
│  (Kotlin/Compose)   │                              │  (Existing + DRF)    │
│                     │                              │                      │
│  Retrofit + OkHttp  │                              │  REST API endpoints  │
│  Room (offline)     │                              │  JWT auth            │
│  MVVM + Flow        │                              │  PostgreSQL          │
│  Coil (images)      │                              │  FCC sync (server)   │
└─────────────────────┘                              └──────────────────────┘
```

**Key principle**: The Android app **never** connects directly to PostgreSQL. A REST API middleware handles all data access, authentication, and business logic securely. Database credentials embedded in an APK can be decompiled — this is non-negotiable.

---

## Phase 1: Django Backend — REST API Layer

Add a REST API to the existing Django project so the Android app can read/write data.

### New Dependencies

Add to `requirements_django.txt`:

| Library | Purpose |
|---------|---------|
| `djangorestframework` | REST framework |
| `djangorestframework-simplejwt` | JWT token authentication |
| `django-cors-headers` | CORS for mobile clients |
| `django-filter` | Search and filter backends |
| `drf-spectacular` | OpenAPI / Swagger docs |

### New Files — All Under `radios/api/`

| File | Purpose |
|------|---------|
| `serializers.py` | Model serializers for Radio, Brand, Manufacturer, etc. |
| `views.py` | ViewSets with search, filter, sort matching current UI |
| `urls.py` | Nested router configuration under `/api/` prefix |
| `filters.py` | Django-filter backends for each model |
| `pagination.py` | 50-per-page pagination matching existing list view |
| `permissions.py` | Read: any authenticated user; Write: staff/admin |

### API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/radios/` | List radios (search, filter, sort, paginate) |
| `GET` | `/api/radios/<id>/` | Radio detail with related models |
| `POST` | `/api/radios/` | Create radio (auth required) |
| `PUT/PATCH` | `/api/radios/<id>/` | Update radio (auth required) |
| `DELETE` | `/api/radios/<id>/` | Delete radio (auth required) |
| `GET` | `/api/brands/` | List brands |
| `GET` | `/api/brands/<id>/` | Brand detail |
| `GET` | `/api/manufacturers/` | List manufacturers |
| `GET` | `/api/manufacturers/map/` | Geocoded manufacturer data |
| `GET` | `/api/stats/` | Dashboard statistics |
| `POST` | `/api/token/` | JWT token obtain (login) |
| `POST` | `/api/token/refresh/` | JWT token refresh |
| `GET` | `/api/docs/` | Swagger UI / OpenAPI schema |

### Modified Files

| File | Change |
|------|--------|
| `radio_database/urls.py` | Add `path('api/', include('radios.api.urls'))` |
| `requirements_django.txt` | Add DRF, simplejwt, corsheaders, drf-spectacular, django-filter |
| `.env` / `.env.example` | Add `API_ALLOWED_ORIGINS`, `API_AUTH_REQUIRED` |
| `README.md` | Document API setup instructions |

---

## Phase 2: Android App — Core Infrastructure

### Required Libraries

| Library | Purpose |
|---------|---------|
| **Retrofit2** | HTTP client for REST API calls |
| **OkHttp** | HTTP engine + logging interceptor |
| **Kotlinx Serialization** | JSON parsing (first-party Kotlin) |
| **Room** | Local SQLite cache for offline browsing |
| **Jetpack Compose + Material 3** | Declarative UI framework |
| **Navigation Compose** | Screen routing |
| **Hilt** | Dependency injection |
| **Coil** | Image loading (FCC docs, radio photos) |
| **DataStore** | Preferences (server URL, auth tokens) |
| **Kotlin Coroutines + Flow** | Async operations + reactive state |
| **WorkManager** | Background data refresh |
| **Google Maps Compose** | Manufacturer map view |

### Android Project Structure

```
radio-tracker-android/
├── app/
│   ├── src/main/
│   │   ├── java/com/radiotracker/
│   │   │   ├── RadioTrackerApp.kt              # @HiltAndroidApp
│   │   │   ├── MainActivity.kt                 # Single Activity host
│   │   │   │
│   │   │   ├── data/
│   │   │   │   ├── remote/
│   │   │   │   │   ├── ApiService.kt           # Retrofit interface
│   │   │   │   │   ├── AuthInterceptor.kt      # OkHttp interceptor for JWT
│   │   │   │   │   └── dto/                    # API response DTOs
│   │   │   │   ├── local/
│   │   │   │   │   ├── AppDatabase.kt          # Room database
│   │   │   │   │   ├── dao/                    # Room DAOs
│   │   │   │   │   └── entity/                 # Room cache entities
│   │   │   │   ├── repository/                 # Repository layer
│   │   │   │   └── datastore/
│   │   │   │       └── AppPreferences.kt       # DataStore wrapper
│   │   │   │
│   │   │   ├── domain/
│   │   │   │   └── model/                      # Domain models
│   │   │   │
│   │   │   ├── ui/
│   │   │   │   ├── navigation/
│   │   │   │   │   ├── NavGraph.kt
│   │   │   │   │   └── Routes.kt               # Route sealed class
│   │   │   │   ├── theme/                      # Material 3 theme
│   │   │   │   ├── components/                 # Reusable composables
│   │   │   │   └── screens/
│   │   │   │       ├── dashboard/              # Stats, recent radios
│   │   │   │       ├── radios/                 # List, Detail, Add/Edit
│   │   │   │       ├── brands/                 # List, Detail, Add/Edit
│   │   │   │       ├── manufacturers/          # List, Map
│   │   │   │       ├── settings/               # Server URL, auth, theme
│   │   │   │       └── sync/                   # FCC sync triggers
│   │   │   │
│   │   │   └── di/                             # Hilt modules
│   │   │       ├── NetworkModule.kt
│   │   │       ├── DatabaseModule.kt
│   │   │       └── DataStoreModule.kt
│   │   │
│   │   └── res/                                # Resources
│   │
│   ├── build.gradle.kts
│   └── proguard-rules.pro
│
├── build.gradle.kts                             # Root build file
├── settings.gradle.kts
└── gradle.properties
```

### Data Layer Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     UI Layer (Compose)                    │
│  ViewModel ← Flow → Composable Screen                    │
└──────────────────┬──────────────────────────────────────┘
                   │  suspend fun / Flow
┌──────────────────▼──────────────────────────────────────┐
│                 Repository Layer                          │
│  Single source of truth: NetworkFirst then Room cache     │
│  RadioRepository, BrandRepository, ManufacturerRepository │
└─────────────┬──────────────────────┬────────────────────┘
              │                      │
    ┌─────────▼─────────┐   ┌───────▼────────┐
    │   Remote Data      │   │   Local Cache   │
    │   (Retrofit/API)   │   │   (Room/SQLite) │
    └───────────────────┘   └────────────────┘
```

---

## Phase 3: Settings & Configuration

The Android app must be configurable to point at any remote Django instance.

### Settings Screen Features

| Feature | Description |
|---------|-------------|
| **Server URL** | Text field with URL validation (e.g., `https://radios.example.com`) |
| **Login** | Username/password form → obtains JWT token pair |
| **Connection Test** | Pings `/api/` to verify reachability |
| **Offline Mode** | Toggle — browse cached Room data without network |
| **Theme** | Radio group: System / Light / Dark |
| **About** | App version, build info, GitHub link |

### DataStore Keys (Persisted Preferences)

| Key | Type | Purpose |
|-----|------|---------|
| `server_url` | `String` | Base API URL |
| `access_token` | `String?` | JWT access token |
| `refresh_token` | `String?` | JWT refresh token |
| `last_sync_at` | `Long?` | Last background sync timestamp |
| `theme_mode` | `Enum` | SYSTEM / LIGHT / DARK |
| `offline_mode` | `Boolean` | Offline-only browsing |

---

## Phase 4: UI Screens

Mapping from existing Django templates to Android Compose screens:

| Screen | Django Template | Compose Implementation |
|--------|----------------|----------------------|
| **Dashboard** | `dashboard.html` | Stats cards (total radios, brands), recent radios list, sync trigger button |
| **Radio List** | `radio_list.html` | Search bar, filter chips (brand, type), paginated `LazyColumn` |
| **Radio Detail** | `radio_detail.html` | Specs table, FCC links, image carousel, OET documents, white-label lineage |
| **Radio Edit/Add** | `radio_form.html` | Scrollable form with field groups (basic info, tech specs, features, notes) |
| **Brand List** | `brand_list.html` | Search bar, sorted `LazyColumn` with FCC grantee codes |
| **Brand Edit/Add** | `brand_form.html` | Form fields (name, alias, grantee code, parent brand) |
| **Manufacturer List** | `manufacturer_list.html` | Search, `LazyColumn` with country badges |
| **Manufacturer Map** | `manufacturer_geomap.html` | Google Maps Compose with geocoded manufacturer markers |
| **Settings** | — (new) | Server URL, login form, theme picker, about section |
| **Sync Panel** | (dashboard section) | FCC ID input field, "Sync All Grantees" button, progress indicator |

---

## Build Order (Dependency Chain)

```
Phase 1 (Django REST API)
     │
     ▼
Phase 2a (Android: networking, auth, DI, Room, DataStore)
     │
     ▼
Phase 3 (Settings screen + configuration)
     │
     ▼
Phase 2b (Android: UI screens — parallelizable)
     ├── Dashboard
     ├── RadioList + RadioDetail + RadioEdit
     ├── BrandList + BrandEdit
     ├── ManufacturerList + ManufacturerMap
     ├── SyncPanel
     └── Offline caching polish
     │
     ▼
Phase 5 (Testing + APK build)
```

### Parallel Groups

- **Phase 2a blocks everything** — networking layer must exist before any screen can fetch data
- **Phase 3 depends on 2a** — Settings needs the auth and preferences infrastructure
- **Phase 2b screens are independent** — once 2a is done, all screens can be built in parallel

---

## Scope — v1 Boundaries

### ✅ Included

- Read & browse: radios (list, detail, search, filter), brands, manufacturers
- JWT authentication: login, auto token refresh, logout
- Settings: configurable server URL, theme toggle, offline mode
- Room offline cache: browse previously loaded data without network
- Basic write operations: add/edit radios (authenticated users)
- Trigger FCC sync from app (executes on server)
- Dark / Light / System theme (Material You)
- Manufacturer map view (Google Maps)
- APK built with `./gradlew assembleRelease`

### ❌ Excluded from v1

| Feature | Reason |
|---------|--------|
| Direct FCC sync on device | Playwright/server-side only |
| Push notifications (FCM) | Requires Firebase setup, can add later |
| Biometric auth | Additional complexity, can add later |
| Camera barcode/FCC ID scanning | Hardware feature, can add later |
| Tablet-specific layouts | Responsive only in v1, dedicated layouts later |
| OET document download to device | Large files, streaming concerns |
| Data export (CSV) | Low usage |
| iOS version | Android-only for now |

---

## Verification Plan

### Backend (Django API)

1. Start Django dev server
2. `curl` each `/api/` endpoint — verify JSON response structure
3. Test search/filter/pagination query parameters
4. Test JWT auth: obtain token, access protected endpoints, verify 401 on expired token
5. Test token refresh flow
6. Verify Swagger UI renders at `/api/docs/`

### Android (Unit Tests)

1. **Networking**: MockWebServer tests for each Retrofit endpoint
2. **AuthInterceptor**: Verify JWT header attached, token refresh on 401
3. **Repository**: Test NetworkFirst strategy — API returns data, Room caches it
4. **DataStore**: Read/write preferences, migration on version change

### Android (UI Tests)

1. Each screen: test loading state, data state, empty state, error state
2. Navigation: verify all route transitions
3. Settings: server URL validation, login flow, theme switching

### Integration

1. Point Android app at local Django server on same Wi-Fi
2. Verify full read flow: Dashboard → Radio List → Radio Detail
3. Verify write flow: Edit radio → save → verify changes persist
4. Verify offline: enable airplane mode → browse cached data
5. Verify FCC sync: enter FCC ID → verify server processes it

### APK Build

```bash
./gradlew assembleRelease
# Signed APK at app/build/outputs/apk/release/
```

---

## Open Decisions (Before Implementation)

1. **Minimum Android SDK**: API 26 (Android 8.0) covers ~97% of devices. Lower increases backward-compat work.
2. **App package name**: e.g., `com.radiotracker.app` or `net.cyberthreatgurl.radiotracker`
3. **Offline strategy**: Full offline (cache all data) vs. online-first with recent cache
4. **FCC sync**: App triggers sync on server — never runs Playwright on the device
5. **Image hosting**: Django serves from local filesystem — needs Nginx/Apache or S3 to make images accessible to mobile clients
````

You can save this to `ANDROID_VERSION.md` in the project root. Want me to adjust anything in the plan before you do?