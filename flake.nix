{
  description = "gluck-calendar — authenticated calendar API behind kelliher-web";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    gluck-service-lib.url = "github:jack-work/gluck-service-lib";
    gluck-service-lib.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    { self, nixpkgs, gluck-service-lib, ... }:
    let
      nixosModule =
        { config, lib, pkgs, ... }:
        let
          cfg = config.services.gluck-calendar;
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

          # One call. Systemd unit, Caddy site, lldap group, Authelia
          # gating — all derived. If the shape ever changes, it changes
          # once in gluck-service-lib.
          config = lib.mkIf cfg.enable (
            gluck-service-lib.lib.mkPythonService {
              inherit config lib pkgs;
              name = "gluck-calendar";
              subdomain = "cal";
              port = cfg.port;
              entrypoint = ./calendar/gluck_calendar.py;
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
              };
              requireAuth = true;
              requiredGroups = [ "gluck-calendar-create" ];
            }
          );
        };
    in
    {
      nixosModules.default = nixosModule;
    };
}
