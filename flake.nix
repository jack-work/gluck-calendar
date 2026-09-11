{
  description = "gluck-calendar — authenticated calendar API behind kelliher-web";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    gluck-service-lib.url = "github:jack-work/gluck-service-lib";
    gluck-service-lib.inputs.nixpkgs.follows = "nixpkgs";
    zanni.url = "github:jack-work/zanni";
  };

  outputs =
    { self, nixpkgs, gluck-service-lib, zanni, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];

      # docs/notify.md
      mkNotify =
        pkgs:
        pkgs.stdenv.mkDerivation {
          pname = "kcal-notify";
          version = "0.1.0";
          src = ./.;
          dontConfigure = true;
          dontBuild = true;
          installPhase = ''
            runHook preInstall
            mkdir -p $out
            cp notify/kcal_notify.py notify/reminders.py calendar/monthview.py $out/
            ${pkgs.python3.withPackages (ps: [ ps.duckdb ps.requests ps.pytz ps.python-dateutil ])}/bin/python \
              tests/test_notify.py
            runHook postInstall
          '';
        };

      # docs/ui.md § Where the defs come from
      mkApp =
        pkgs:
        pkgs.stdenv.mkDerivation {
          pname = "gluck-calendar";
          version = "0.2.0";
          src = ./.;
          nativeBuildInputs = [
            zanni.packages.${pkgs.stdenv.hostPlatform.system}.default
            pkgs.nodejs
          ];
          dontConfigure = true;
          dontBuild = true;
          installPhase = ''
            runHook preInstall
            mkdir -p $out/templates
            cp calendar/gluck_calendar.py calendar/monthview.py $out/
            zanni-inline \
              --component boil --component gesso \
              --component phosphor --component fontpack \
              calendar/templates/month.html.in -o $out/templates/month.html
            zanni-check $out/templates/month.html
            node bin/cal-check $out/templates/month.html
            runHook postInstall
          '';
        };


      # kcal-notify: the reminders, as their own unit. It reads the calendar
      # over its HTTP API as a read-only service client and says to herald,
      # so it shares no state with the web app and opens no database of the
      # calendar's. docs/notify.md.
      notifyModule =
        { config, lib, pkgs, ... }:
        let
          cfg = config.services.kcal-notify;
          app = mkNotify pkgs;
          py = pkgs.python3.withPackages (ps: with ps; [ duckdb requests pytz python-dateutil ]);
        in
        {
          options.services.kcal-notify = {
            enable = lib.mkEnableOption "calendar reminders over herald";

            secretFile = lib.mkOption {
              type = lib.types.path;
              description = ''
                Path to the kcal-notify OIDC client secret. Delivered through
                LoadCredential, never through the environment.
              '';
            };

            recipient = lib.mkOption {
              type = lib.types.str;
              default = "gluck";
              description = "herald route name to deliver to.";
            };

            readAs = lib.mkOption {
              type = lib.types.str;
              default = "gluck";
              description = ''
                Whose calendar is summarised. Must match the username this
                client is mapped to in services.gluck-calendar.serviceClients.
              '';
            };

            calendarUrl = lib.mkOption {
              type = lib.types.str;
              default = "http://127.0.0.1:9094";
            };

            heraldUrl = lib.mkOption {
              type = lib.types.str;
              default = "http://127.0.0.1:9098";
            };

            clientId = lib.mkOption {
              type = lib.types.str;
              default = "kcal-notify";
            };

            timeZone = lib.mkOption {
              type = lib.types.str;
              default = "America/New_York";
            };

            schedule = lib.mkOption {
              type = lib.types.str;
              default = "minutely";
              description = ''
                OnCalendar expression. Reminder resolution equals this
                interval: a lead warning lands within one firing of T-60.
              '';
            };
          };

          config = lib.mkIf cfg.enable (
            gluck-service-lib.lib.mkScheduledJob {
              inherit config lib pkgs;
              name = "kcal-notify";
              description = "calendar reminders over herald";
              schedule = cfg.schedule;
              # A random delay would defeat the point: the resolution of every
              # reminder is this timer.
              randomizedDelaySec = "0";
              accuracySec = "1s";
              execStart = "${py}/bin/python ${app}/kcal_notify.py";
              stateDirectory = "kcal-notify";
              timeoutStartSec = "2m";
              credentials.herald-client-secret = cfg.secretFile;
              environment = {
                KCAL_NOTIFY_DB = "/var/lib/kcal-notify/reminders.duckdb";
                KCAL_NOTIFY_TZ = cfg.timeZone;
                KCAL_NOTIFY_TO = cfg.recipient;
                KCAL_NOTIFY_USER = cfg.readAs;
                KCAL_NOTIFY_CLIENT_ID = cfg.clientId;
                KCAL_NOTIFY_CALENDAR_URL = cfg.calendarUrl;
                KCAL_NOTIFY_HERALD_URL = cfg.heraldUrl;
              };
              after = [ "gluck-calendar.service" "gluck-herald.service" ];
            }
          );
        };

      nixosModule =
        { config, lib, pkgs, ... }:
        let
          cfg = config.services.gluck-calendar;
          app = mkApp pkgs;
        in
        {
          options.services.gluck-calendar = {
            enable = lib.mkEnableOption "gluck-calendar API (DuckDB events + per-item ACLs)";

            port = lib.mkOption {
              type = lib.types.port;
              default = 9094;
              description = "Loopback port for the calendar API";
            };

            timeZone = lib.mkOption {
              type = lib.types.str;
              default = "America/New_York";
              example = "Europe/Madrid";
              description = ''
                IANA zone the web view renders in. Events are stored as
                TIMESTAMPTZ and the API is unaffected; this decides which
                civil day an instance lands on in the month grid.
              '';
            };

            serviceClients = lib.mkOption {
              type = lib.types.attrsOf lib.types.str;
              default = { };
              example = { kcal-notify = "gluck"; };
              description = ''
                Machine callers, as client_id -> the user whose events they
                may read.

                The grant is read-only: any method other than GET is
                refused before routing.
              '';
            };
          };

          config = lib.mkIf cfg.enable (
            gluck-service-lib.lib.mkPythonService {
              inherit config lib pkgs;
              name = "gluck-calendar";
              subdomain = "cal";
              port = cfg.port;
              entrypoint = "${app}/gluck_calendar.py";
              pythonPackages = ps: with ps; [
                flask
                waitress
                requests
                duckdb
                pyjwt
                cryptography
                python-dateutil
                pytz
              ];
              stateDirectory = "gluck-calendar";
              environment = {
                GLUCK_CALENDAR_DB = "/var/lib/gluck-calendar/calendar.duckdb";
                GLUCK_CALENDAR_SERVICE_CLIENTS = builtins.toJSON cfg.serviceClients;
                GLUCK_CALENDAR_TZ = cfg.timeZone;
              };
              requireAuth = true;
              requiredGroups = [ "calendar-create" ];
            }
          );
        };
    in
    {
      nixosModules.default = nixosModule;
      nixosModules.notify = notifyModule;

      packages = nixpkgs.lib.genAttrs systems (
        system: {
          default = mkApp nixpkgs.legacyPackages.${system};
          notify = mkNotify nixpkgs.legacyPackages.${system};
        }
      );

      checks = nixpkgs.lib.genAttrs systems (
        system: {
          ui = mkApp nixpkgs.legacyPackages.${system};
          notify = mkNotify nixpkgs.legacyPackages.${system};
        }
      );
    };
}
