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

      packages = nixpkgs.lib.genAttrs systems (
        system: { default = mkApp nixpkgs.legacyPackages.${system}; }
      );

      checks = nixpkgs.lib.genAttrs systems (
        system: { ui = mkApp nixpkgs.legacyPackages.${system}; }
      );
    };
}
