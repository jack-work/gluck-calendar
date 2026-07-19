{
  description = "gluck-calendar — authenticated calendar API behind kelliher-web";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs, ... }:
    let
      nixosModule =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.services.gluck-calendar;
          siteCfg = config.services.gluck-calendar-site;
          py = pkgs.python3.withPackages (
            ps: with ps; [
              flask
              waitress
              requests
              duckdb
              pyjwt
              cryptography
              python-dateutil
              pytz
            ]
          );
          hardened = {
            NoNewPrivileges = true;
            PrivateTmp = true;
            PrivateDevices = true;
            ProtectHome = true;
            ProtectSystem = "strict";
            ProtectKernelTunables = true;
            ProtectKernelModules = true;
            ProtectKernelLogs = true;
            ProtectControlGroups = true;
            RestrictAddressFamilies = [
              "AF_INET"
              "AF_INET6"
              "AF_UNIX"
            ];
            RestrictNamespaces = true;
            RestrictRealtime = true;
            RestrictSUIDSGID = true;
            LockPersonality = true;
            SystemCallArchitectures = "native";
          };
        in
        {
          options.services.gluck-calendar = {
            enable = lib.mkEnableOption "gluck-calendar API (DuckDB events + per-item ACLs)";

            port = lib.mkOption {
              type = lib.types.port;
              default = 9094;
              description = "Loopback port for the calendar API";
            };
          };

          options.services.gluck-calendar-site = {
            subdomains = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ "cal" ];
              description = ''
                Subdomain labels for the calendar API. Expanded across
                `services.kelliher-web.baseDomains` at the platform.
                Defaults to `cal` (→ `cal.<baseDomain>`).
              '';
            };
            extraHostnames = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ ];
              description = "Extra fully-qualified hostnames merged into the site.";
            };
          };

          config = lib.mkIf cfg.enable {
            systemd.services.gluck-calendar = {
              description = "gluck-calendar — DuckDB calendar API with per-item ACLs";
              after = [ "network.target" ];
              wantedBy = [ "multi-user.target" ];
              environment = {
                GLUCK_CALENDAR_DB = "/var/lib/gluck-calendar/calendar.duckdb";
                PORT = toString cfg.port;
              };
              serviceConfig = hardened // {
                DynamicUser = true;
                StateDirectory = "gluck-calendar";
                ExecStart = "${py}/bin/python ${./calendar/gluck_calendar.py}";
                Restart = "on-failure";
                RestartSec = 5;
              };
            };

            services.kelliher-web.sites.gluck-calendar = {
              subdomains = siteCfg.subdomains;
              hostnames = siteCfg.extraHostnames;
              requireAuth = true;
              proxyTo = cfg.port;
            };
          };
        };
    in
    {
      nixosModules.default = nixosModule;
    };
}
