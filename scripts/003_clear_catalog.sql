-- Forward-only migration: define clear_catalog() without invoking it.
-- Apply to an existing catalogue with:
--   psql -v ON_ERROR_STOP=1 "$IMAGELIB_DSN" -f scripts/003_clear_catalog.sql

\set ON_ERROR_STOP on

BEGIN;

CREATE OR REPLACE FUNCTION public.clear_catalog()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $clear_catalog$
DECLARE
    required_table TEXT;
    table_name TEXT;
    sequence_name TEXT;
BEGIN
    FOREACH required_table IN ARRAY ARRAY[
        'sources',
        'images',
        'faces',
        'persons',
        'face_match_decisions'
    ] LOOP
        IF to_regclass(format('public.%I', required_table)) IS NULL THEN
            RAISE EXCEPTION
                'Cannot clear catalogue: required table public.% is missing',
                required_table;
        END IF;
    END LOOP;

    LOCK TABLE public.sources,
        public.images,
        public.faces,
        public.persons,
        public.face_match_decisions
        IN ACCESS EXCLUSIVE MODE;

    IF to_regclass('public.image_locations') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE public.image_locations IN ACCESS EXCLUSIVE MODE';
        EXECUTE 'DELETE FROM public.image_locations';
    END IF;

    DELETE FROM public.face_match_decisions;
    DELETE FROM public.faces;
    DELETE FROM public.persons;
    DELETE FROM public.images;
    DELETE FROM public.sources;

    FOREACH table_name IN ARRAY ARRAY['sources', 'images', 'faces', 'persons'] LOOP
        sequence_name := pg_get_serial_sequence(format('public.%I', table_name), 'id');
        IF sequence_name IS NOT NULL THEN
            EXECUTE format('ALTER SEQUENCE %s RESTART WITH 1', sequence_name::regclass);
        END IF;
    END LOOP;

    IF to_regclass('public.image_locations') IS NOT NULL THEN
        sequence_name := pg_get_serial_sequence('public.image_locations', 'id');
        IF sequence_name IS NOT NULL THEN
            EXECUTE format('ALTER SEQUENCE %s RESTART WITH 1', sequence_name::regclass);
        END IF;
    END IF;
END;
$clear_catalog$;

COMMIT;
