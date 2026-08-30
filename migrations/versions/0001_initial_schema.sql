--
-- PostgreSQL database dump
--


-- Dumped from database version 18.3
-- Dumped by pg_dump version 18.3

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET search_path = public;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: rdkit; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS rdkit WITH SCHEMA public;

-- pg_restore clears search_path while loading data. RDKit 4.8 implements the
-- text overload as SQL that calls its cstring overload without qualification,
-- so topology COPY otherwise fails while evaluating generated fingerprints.
ALTER FUNCTION public.mol_from_smiles(text) SET search_path = public, pg_catalog;


--
-- Name: geometry_internal_coordinates_equivalent(double precision[], double precision[], double precision[], smallint, double precision[], double precision[], double precision[], smallint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.geometry_internal_coordinates_equivalent(candidate_distances double precision[], candidate_angles double precision[], candidate_dihedrals double precision[], candidate_minimum_coordinate_decimal_places smallint, observed_distances double precision[], observed_angles double precision[], observed_dihedrals double precision[], observed_coordinate_decimal_places smallint) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
    AS $$
DECLARE
    coordinate_tolerance double precision;
    distance_tolerance double precision;
    angular_tolerance double precision;
    length_scale double precision;
    minimum_positive_distance double precision;
    candidate_length integer;
    observed_length integer;
    known_places integer;
    coordinate_index integer;
    candidate_distance double precision;
    observed_distance double precision;
    candidate_angle double precision;
    observed_angle double precision;
    dihedral_delta double precision;
    candidate_is_linear boolean;
    observed_is_linear boolean;
BEGIN
    IF candidate_distances IS NULL
       OR candidate_angles IS NULL
       OR candidate_dihedrals IS NULL
       OR observed_distances IS NULL
       OR observed_angles IS NULL
       OR observed_dihedrals IS NULL THEN
        RETURN false;
    END IF;

    candidate_length := cardinality(candidate_distances);
    observed_length := cardinality(observed_distances);
    IF candidate_length IS NULL
       OR candidate_length = 0
       OR candidate_length <> observed_length
       OR cardinality(candidate_angles) <> candidate_length
       OR cardinality(candidate_dihedrals) <> candidate_length
       OR cardinality(observed_angles) <> observed_length
       OR cardinality(observed_dihedrals) <> observed_length THEN
        RETURN false;
    END IF;

    IF candidate_minimum_coordinate_decimal_places IS NULL
       AND observed_coordinate_decimal_places IS NULL THEN
        coordinate_tolerance := 1e-6;
    ELSE
        known_places := LEAST(
            COALESCE(candidate_minimum_coordinate_decimal_places, 18),
            COALESCE(observed_coordinate_decimal_places, 18)
        );
        coordinate_tolerance := GREATEST(1e-8, 1.1 * power(10.0, -known_places));
    END IF;
    distance_tolerance := 2.2 * coordinate_tolerance;

    FOR coordinate_index IN 1..candidate_length LOOP
        candidate_distance := candidate_distances[coordinate_index];
        observed_distance := observed_distances[coordinate_index];
        IF candidate_distance IS NULL OR observed_distance IS NULL
           OR candidate_distance IN (
               'NaN'::double precision,
               'Infinity'::double precision,
               '-Infinity'::double precision
           )
           OR observed_distance IN (
               'NaN'::double precision,
               'Infinity'::double precision,
               '-Infinity'::double precision
           )
           OR abs(candidate_distance - observed_distance) > distance_tolerance THEN
            RETURN false;
        END IF;
        IF candidate_distance > 1e-8
           AND (
               minimum_positive_distance IS NULL
               OR candidate_distance < minimum_positive_distance
           ) THEN
            minimum_positive_distance := candidate_distance;
        END IF;
        IF observed_distance > 1e-8
           AND (
               minimum_positive_distance IS NULL
               OR observed_distance < minimum_positive_distance
           ) THEN
            minimum_positive_distance := observed_distance;
        END IF;
    END LOOP;

    length_scale := GREATEST(COALESCE(minimum_positive_distance, 1.0), 0.1);
    angular_tolerance := GREATEST(
        1e-6,
        degrees(4.0 * coordinate_tolerance / length_scale)
    );

    FOR coordinate_index IN 1..candidate_length LOOP
        candidate_angle := candidate_angles[coordinate_index];
        observed_angle := observed_angles[coordinate_index];
        IF candidate_angle IS NULL OR observed_angle IS NULL
           OR candidate_angle IN (
               'NaN'::double precision,
               'Infinity'::double precision,
               '-Infinity'::double precision
           )
           OR observed_angle IN (
               'NaN'::double precision,
               'Infinity'::double precision,
               '-Infinity'::double precision
           )
           OR abs(candidate_angle - observed_angle) > angular_tolerance THEN
            RETURN false;
        END IF;

        candidate_is_linear := LEAST(
            abs(candidate_angle),
            abs(180.0 - candidate_angle)
        ) <= angular_tolerance;
        observed_is_linear := LEAST(
            abs(observed_angle),
            abs(180.0 - observed_angle)
        ) <= angular_tolerance;
        IF NOT (candidate_is_linear OR observed_is_linear) THEN
            -- PostgreSQL only overloads mod() for integral/numeric inputs.
            -- This is the float8 equivalent of ((delta + 180) % 360) - 180.
            dihedral_delta := abs(
                candidate_dihedrals[coordinate_index]
                - observed_dihedrals[coordinate_index]
                - 360.0 * floor(
                    (
                        candidate_dihedrals[coordinate_index]
                        - observed_dihedrals[coordinate_index]
                        + 180.0
                    ) / 360.0
                )
            );
            IF candidate_dihedrals[coordinate_index] IS NULL
               OR observed_dihedrals[coordinate_index] IS NULL
               OR candidate_dihedrals[coordinate_index] IN (
                   'NaN'::double precision,
                   'Infinity'::double precision,
                   '-Infinity'::double precision
               )
               OR observed_dihedrals[coordinate_index] IN (
                   'NaN'::double precision,
                   'Infinity'::double precision,
                   '-Infinity'::double precision
               )
               OR dihedral_delta > angular_tolerance THEN
                RETURN false;
            END IF;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: artifact_file; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.artifact_file (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    bucket character varying(255) NOT NULL,
    object_key text NOT NULL,
    version_id text,
    content_sha256 character varying(64) NOT NULL,
    size_bytes bigint NOT NULL,
    original_filename text NOT NULL,
    media_type character varying(255) NOT NULL,
    artifact_kind character varying(18) NOT NULL,
    storage_status character varying(9) DEFAULT 'pending'::character varying NOT NULL,
    etag text,
    storage_verified_at timestamp with time zone,
    project_id uuid NOT NULL,
    visibility character varying(7) DEFAULT 'project'::character varying NOT NULL,
    created_by_user_id uuid NOT NULL,
    CONSTRAINT artifact_file_artifact_kind CHECK (((artifact_kind)::text = ANY ((ARRAY['calculation_output'::character varying, 'input'::character varying, 'workflow_manifest'::character varying, 'auxiliary'::character varying])::text[]))),
    CONSTRAINT artifact_file_storage_status CHECK (((storage_status)::text = ANY ((ARRAY['pending'::character varying, 'available'::character varying, 'missing'::character varying, 'corrupt'::character varying, 'retired'::character varying])::text[]))),
    CONSTRAINT artifact_visibility CHECK (((visibility)::text = ANY ((ARRAY['public'::character varying, 'project'::character varying])::text[]))),
    CONSTRAINT ck_artifact_file_sha256_hex CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_artifact_file_size_nonnegative CHECK ((size_bytes >= 0))
);


--
-- Name: artifact_ingestion; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.artifact_ingestion (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    artifact_file_id uuid NOT NULL,
    status character varying(9) DEFAULT 'pending'::character varying NOT NULL,
    parser_name character varying(64) NOT NULL,
    parser_version character varying(128) NOT NULL,
    source_frame_count integer,
    transition_state_frame_count integer,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    error_code character varying(128),
    error_message text,
    parser_metadata jsonb NOT NULL,
    CONSTRAINT artifact_ingestion_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'succeeded'::character varying, 'partial'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT ck_artifact_ingestion_source_frames_nonnegative CHECK (((source_frame_count IS NULL) OR (source_frame_count >= 0))),
    CONSTRAINT ck_artifact_ingestion_terminal_timestamp CHECK ((((status)::text = 'pending'::text) OR (completed_at IS NOT NULL))),
    CONSTRAINT ck_artifact_ingestion_timestamps_ordered CHECK (((completed_at IS NULL) OR (started_at IS NULL) OR (completed_at >= started_at))),
    CONSTRAINT ck_artifact_ingestion_ts_frames_lte_source CHECK (((source_frame_count IS NULL) OR (transition_state_frame_count IS NULL) OR (transition_state_frame_count <= source_frame_count))),
    CONSTRAINT ck_artifact_ingestion_ts_frames_nonnegative CHECK (((transition_state_frame_count IS NULL) OR (transition_state_frame_count >= 0)))
);


--
-- Name: atomic_population_series; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.atomic_population_series (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    result_id uuid NOT NULL,
    series_key character varying(128) NOT NULL,
    scheme character varying(128) NOT NULL,
    quantity character varying(128) NOT NULL,
    value_count integer NOT NULL,
    spin_channel character varying(16),
    source_label text,
    metadata jsonb NOT NULL,
    CONSTRAINT ck_atomic_population_series_spin_channel CHECK (((spin_channel IS NULL) OR ((spin_channel)::text = ANY ((ARRAY['alpha'::character varying, 'beta'::character varying, 'total'::character varying])::text[])))),
    CONSTRAINT ck_atomic_population_series_value_count_positive CHECK ((value_count > 0))
);


--
-- Name: audit_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_event (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    actor_user_id uuid,
    project_id uuid,
    action character varying(128) NOT NULL,
    entity_type character varying(128) NOT NULL,
    entity_id uuid,
    metadata_json jsonb NOT NULL
);


--
-- Name: auth_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_session (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    user_agent text,
    ip_address character varying(64)
);


--
-- Name: bond_order_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bond_order_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    matrix_count integer NOT NULL,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_bond_order_result_count_nonnegative CHECK ((matrix_count >= 0))
);


--
-- Name: calculation_frame; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.calculation_frame (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    parse_revision_id uuid NOT NULL,
    segment_id uuid NOT NULL,
    frame_index integer NOT NULL,
    file_frame_index integer NOT NULL,
    frame_role character varying(12) NOT NULL,
    source_start_byte bigint NOT NULL,
    source_end_byte bigint NOT NULL,
    source_start_char bigint,
    source_end_char bigint,
    source_start_line integer NOT NULL,
    source_end_line integer NOT NULL,
    source_block_sha256 character varying(64) NOT NULL,
    geometry_id uuid NOT NULL,
    charge smallint NOT NULL,
    multiplicity smallint NOT NULL,
    geometry_assignment_kind character varying(26) NOT NULL,
    observed_coordinate_hash character varying(64) NOT NULL,
    observed_to_geometry_atom_indices integer[] NOT NULL,
    electronic_state_kind character varying(6) DEFAULT 'ground'::character varying NOT NULL,
    electronic_state_index smallint DEFAULT '0'::smallint NOT NULL,
    scf_status character varying(13) DEFAULT 'unknown'::character varying NOT NULL,
    optimization_status character varying(13) DEFAULT 'unknown'::character varying NOT NULL,
    electronic_total_energy_hartree numeric(24,6),
    reference_total_energy_hartree numeric(24,6),
    mp2_total_energy_hartree numeric(24,6),
    mp3_total_energy_hartree numeric(24,6),
    mp4_total_energy_hartree numeric(24,6),
    mp5_total_energy_hartree numeric(24,6),
    ccsd_total_energy_hartree numeric(24,6),
    ccsd_t_total_energy_hartree numeric(24,6),
    selected_energy_hartree numeric(24,6),
    selected_energy_kind character varying(31),
    energy_selection_policy_version character varying(64),
    energy_change_hartree double precision,
    energy_change_threshold_hartree double precision,
    energy_change_converged boolean,
    rms_force_hartree_per_bohr double precision,
    rms_force_threshold_hartree_per_bohr double precision,
    rms_force_converged boolean,
    max_force_hartree_per_bohr double precision,
    max_force_threshold_hartree_per_bohr double precision,
    max_force_converged boolean,
    rms_displacement_bohr double precision,
    rms_displacement_threshold_bohr double precision,
    rms_displacement_converged boolean,
    max_displacement_bohr double precision,
    max_displacement_threshold_bohr double precision,
    max_displacement_converged boolean,
    frequency_count integer,
    negative_frequency_count integer,
    lowest_frequency_cm1 double precision,
    program_metadata_schema_version character varying(64) DEFAULT 'calculation-frame-metadata-v1'::character varying NOT NULL,
    program_metadata jsonb NOT NULL,
    parse_presence jsonb DEFAULT '{}'::jsonb NOT NULL,
    parse_completeness character varying(12) DEFAULT 'not_assessed'::character varying NOT NULL,
    parse_diagnostics jsonb DEFAULT '[]'::jsonb NOT NULL,
    running_time_seconds double precision,
    topology_derivation_id uuid NOT NULL,
    coordinate_decimal_places smallint,
    geometry_assignment_rmsd_angstrom double precision NOT NULL,
    geometry_assignment_max_abs_angstrom double precision NOT NULL,
    geometry_assignment_policy_version character varying(64) NOT NULL,
    observed_coordinates bytea NOT NULL,
    observed_to_geometry_transform double precision[] NOT NULL,
    CONSTRAINT calculation_frame_electronic_state_kind CHECK (((electronic_state_kind)::text = 'ground'::text)),
    CONSTRAINT calculation_frame_geometry_assignment_kind CHECK (((geometry_assignment_kind)::text = ANY ((ARRAY['parsed_exact'::character varying, 'matched_existing_geometry'::character varying])::text[]))),
    CONSTRAINT calculation_frame_optimization_status CHECK (((optimization_status)::text = ANY ((ARRAY['not_requested'::character varying, 'not_converged'::character varying, 'converged'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT calculation_frame_parse_completeness CHECK (((parse_completeness)::text = ANY ((ARRAY['not_assessed'::character varying, 'complete'::character varying, 'partial'::character varying])::text[]))),
    CONSTRAINT calculation_frame_role CHECK (((frame_role)::text = ANY ((ARRAY['initial'::character varying, 'intermediate'::character varying, 'terminal'::character varying, 'single_point'::character varying])::text[]))),
    CONSTRAINT calculation_frame_scf_status CHECK (((scf_status)::text = ANY ((ARRAY['not_requested'::character varying, 'converged'::character varying, 'failed'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT calculation_frame_selected_energy_kind CHECK (((selected_energy_kind)::text = ANY ((ARRAY['electronic_total_energy_hartree'::character varying, 'reference_total_energy_hartree'::character varying, 'mp2_total_energy_hartree'::character varying, 'mp3_total_energy_hartree'::character varying, 'mp4_total_energy_hartree'::character varying, 'mp5_total_energy_hartree'::character varying, 'ccsd_total_energy_hartree'::character varying, 'ccsd_t_total_energy_hartree'::character varying])::text[]))),
    CONSTRAINT ck_calculation_frame_assignment_max_abs_ge_rmsd CHECK (((geometry_assignment_max_abs_angstrom IS NULL) OR (geometry_assignment_max_abs_angstrom >= geometry_assignment_rmsd_angstrom))),
    CONSTRAINT ck_calculation_frame_assignment_rmsd_nonnegative CHECK (((geometry_assignment_rmsd_angstrom IS NULL) OR (geometry_assignment_rmsd_angstrom >= (0)::double precision))),
    CONSTRAINT ck_calculation_frame_block_hash_hex CHECK (((source_block_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_calculation_frame_byte_span CHECK (((source_start_byte >= 0) AND (source_end_byte > source_start_byte))),
    CONSTRAINT ck_calculation_frame_char_span CHECK (((num_nonnulls(source_start_char, source_end_char) = 0) OR ((num_nonnulls(source_start_char, source_end_char) = 2) AND (source_start_char >= 0) AND (source_end_char > source_start_char)))),
    CONSTRAINT ck_calculation_frame_coordinate_decimal_places CHECK (((coordinate_decimal_places IS NULL) OR ((coordinate_decimal_places >= 0) AND (coordinate_decimal_places <= 18)))),
    CONSTRAINT ck_calculation_frame_energy_change_convergence_inputs CHECK (((energy_change_converged IS NULL) OR (num_nonnulls(energy_change_hartree, energy_change_threshold_hartree) = 2))),
    CONSTRAINT ck_calculation_frame_energy_change_threshold CHECK (((energy_change_threshold_hartree IS NULL) OR (energy_change_threshold_hartree >= (0)::double precision))),
    CONSTRAINT ck_calculation_frame_file_index_nonnegative CHECK ((file_frame_index >= 0)),
    CONSTRAINT ck_calculation_frame_frequency_count_nonnegative CHECK (((frequency_count IS NULL) OR (frequency_count >= 0))),
    CONSTRAINT ck_calculation_frame_frequency_summary_complete CHECK ((((frequency_count IS NULL) AND (negative_frequency_count IS NULL) AND (lowest_frequency_cm1 IS NULL)) OR ((frequency_count = 0) AND (NOT (negative_frequency_count IS DISTINCT FROM 0)) AND (lowest_frequency_cm1 IS NULL)) OR ((frequency_count > 0) AND (negative_frequency_count IS NOT NULL) AND (lowest_frequency_cm1 IS NOT NULL)))),
    CONSTRAINT ck_calculation_frame_ground_state_v1 CHECK ((((electronic_state_kind)::text = 'ground'::text) AND (electronic_state_index = 0))),
    CONSTRAINT ck_calculation_frame_index_nonnegative CHECK ((frame_index >= 0)),
    CONSTRAINT ck_calculation_frame_line_span CHECK (((source_start_line >= 1) AND (source_end_line > source_start_line))),
    CONSTRAINT ck_calculation_frame_matched_geometry_evidence CHECK ((num_nonnulls(observed_coordinates, observed_coordinate_hash, observed_to_geometry_atom_indices, observed_to_geometry_transform, geometry_assignment_rmsd_angstrom, geometry_assignment_max_abs_angstrom, geometry_assignment_policy_version) = 7)),
    CONSTRAINT ck_calculation_frame_max_displacement_convergence_inputs CHECK (((max_displacement_converged IS NULL) OR (num_nonnulls(max_displacement_bohr, max_displacement_threshold_bohr) = 2))),
    CONSTRAINT ck_calculation_frame_max_displacement_threshold CHECK (((max_displacement_threshold_bohr IS NULL) OR (max_displacement_threshold_bohr >= (0)::double precision))),
    CONSTRAINT ck_calculation_frame_max_force_convergence_inputs CHECK (((max_force_converged IS NULL) OR (num_nonnulls(max_force_hartree_per_bohr, max_force_threshold_hartree_per_bohr) = 2))),
    CONSTRAINT ck_calculation_frame_max_force_threshold CHECK (((max_force_threshold_hartree_per_bohr IS NULL) OR (max_force_threshold_hartree_per_bohr >= (0)::double precision))),
    CONSTRAINT ck_calculation_frame_multiplicity_positive CHECK ((multiplicity > 0)),
    CONSTRAINT ck_calculation_frame_negative_frequency_count_lte_total CHECK (((frequency_count IS NULL) OR (negative_frequency_count IS NULL) OR (negative_frequency_count <= frequency_count))),
    CONSTRAINT ck_calculation_frame_negative_frequency_count_nonnegative CHECK (((negative_frequency_count IS NULL) OR (negative_frequency_count >= 0))),
    CONSTRAINT ck_calculation_frame_observed_hash_hex CHECK (((observed_coordinate_hash IS NULL) OR ((observed_coordinate_hash)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_calculation_frame_observed_transform_length CHECK ((cardinality(observed_to_geometry_transform) = 16)),
    CONSTRAINT ck_calculation_frame_rms_displacement_convergence_inputs CHECK (((rms_displacement_converged IS NULL) OR (num_nonnulls(rms_displacement_bohr, rms_displacement_threshold_bohr) = 2))),
    CONSTRAINT ck_calculation_frame_rms_displacement_threshold CHECK (((rms_displacement_threshold_bohr IS NULL) OR (rms_displacement_threshold_bohr >= (0)::double precision))),
    CONSTRAINT ck_calculation_frame_rms_force_convergence_inputs CHECK (((rms_force_converged IS NULL) OR (num_nonnulls(rms_force_hartree_per_bohr, rms_force_threshold_hartree_per_bohr) = 2))),
    CONSTRAINT ck_calculation_frame_rms_force_threshold CHECK (((rms_force_threshold_hartree_per_bohr IS NULL) OR (rms_force_threshold_hartree_per_bohr >= (0)::double precision))),
    CONSTRAINT ck_calculation_frame_running_time_nonnegative CHECK (((running_time_seconds IS NULL) OR (running_time_seconds >= (0)::double precision))),
    CONSTRAINT ck_calculation_frame_selected_energy_complete CHECK ((num_nonnulls(selected_energy_hartree, selected_energy_kind, energy_selection_policy_version) = ANY (ARRAY[0, 3]))),
    CONSTRAINT ck_calculation_frame_selected_energy_matches_source CHECK (((selected_energy_kind IS NULL) OR (NOT ((selected_energy_hartree)::double precision IS DISTINCT FROM
CASE
    WHEN ((selected_energy_kind)::text = 'electronic_total_energy_hartree'::text) THEN (electronic_total_energy_hartree)::double precision
    WHEN ((selected_energy_kind)::text = 'reference_total_energy_hartree'::text) THEN (reference_total_energy_hartree)::double precision
    WHEN ((selected_energy_kind)::text = 'mp2_total_energy_hartree'::text) THEN (mp2_total_energy_hartree)::double precision
    WHEN ((selected_energy_kind)::text = 'mp3_total_energy_hartree'::text) THEN (mp3_total_energy_hartree)::double precision
    WHEN ((selected_energy_kind)::text = 'mp4_total_energy_hartree'::text) THEN (mp4_total_energy_hartree)::double precision
    WHEN ((selected_energy_kind)::text = 'mp5_total_energy_hartree'::text) THEN (mp5_total_energy_hartree)::double precision
    WHEN ((selected_energy_kind)::text = 'ccsd_total_energy_hartree'::text) THEN (ccsd_total_energy_hartree)::double precision
    WHEN ((selected_energy_kind)::text = 'ccsd_t_total_energy_hartree'::text) THEN (ccsd_t_total_energy_hartree)::double precision
    ELSE NULL::double precision
END))))
);


--
-- Name: calculation_protocol; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.calculation_protocol (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    protocol_hash character varying(64) NOT NULL,
    spec_schema_version character varying(64) DEFAULT 'calculation-protocol-v1'::character varying NOT NULL,
    qm_software character varying(8) NOT NULL,
    qm_software_version character varying(128) NOT NULL,
    method_family character varying(128),
    method character varying(256),
    reference_method character varying(128),
    functional character varying(128),
    basis_set character varying(256),
    auxiliary_basis_set character varying(256),
    dispersion_model character varying(128),
    solvation_model character varying(128),
    solvent character varying(128),
    relativistic_method character varying(128),
    task_requests text[] NOT NULL,
    normalized_spec jsonb NOT NULL,
    CONSTRAINT calculation_protocol_qm_software CHECK (((qm_software)::text = ANY ((ARRAY['gaussian'::character varying, 'orca'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT ck_calculation_protocol_hash_hex CHECK (((protocol_hash)::text ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: calculation_segment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.calculation_segment (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    parse_revision_id uuid NOT NULL,
    protocol_id uuid,
    segment_index integer NOT NULL,
    segment_label text,
    source_start_byte bigint NOT NULL,
    source_end_byte bigint NOT NULL,
    source_start_char bigint,
    source_end_char bigint,
    source_start_line integer NOT NULL,
    source_end_line integer NOT NULL,
    source_block_sha256 character varying(64) NOT NULL,
    requested_cpu_count integer,
    requested_memory_mb bigint,
    termination_status character varying(10) DEFAULT 'unknown'::character varying NOT NULL,
    scf_status character varying(13) DEFAULT 'unknown'::character varying NOT NULL,
    wall_time_seconds double precision,
    program_metadata jsonb NOT NULL,
    source_frame_count integer,
    parse_presence jsonb DEFAULT '{}'::jsonb NOT NULL,
    parse_completeness character varying(12) DEFAULT 'not_assessed'::character varying NOT NULL,
    parse_diagnostics jsonb DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT calculation_segment_parse_completeness CHECK (((parse_completeness)::text = ANY ((ARRAY['not_assessed'::character varying, 'complete'::character varying, 'partial'::character varying])::text[]))),
    CONSTRAINT calculation_segment_scf_status CHECK (((scf_status)::text = ANY ((ARRAY['not_requested'::character varying, 'converged'::character varying, 'failed'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT calculation_segment_termination_status CHECK (((termination_status)::text = ANY ((ARRAY['normal'::character varying, 'error'::character varying, 'incomplete'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT ck_calculation_segment_block_hash_hex CHECK (((source_block_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_calculation_segment_byte_span CHECK (((source_start_byte >= 0) AND (source_end_byte > source_start_byte))),
    CONSTRAINT ck_calculation_segment_char_span CHECK (((num_nonnulls(source_start_char, source_end_char) = 0) OR ((num_nonnulls(source_start_char, source_end_char) = 2) AND (source_start_char >= 0) AND (source_end_char > source_start_char)))),
    CONSTRAINT ck_calculation_segment_cpu_positive CHECK (((requested_cpu_count IS NULL) OR (requested_cpu_count > 0))),
    CONSTRAINT ck_calculation_segment_index_nonnegative CHECK ((segment_index >= 0)),
    CONSTRAINT ck_calculation_segment_line_span CHECK (((source_start_line >= 1) AND (source_end_line > source_start_line))),
    CONSTRAINT ck_calculation_segment_memory_positive CHECK (((requested_memory_mb IS NULL) OR (requested_memory_mb > 0))),
    CONSTRAINT ck_calculation_segment_source_frame_count_nonnegative CHECK (((source_frame_count IS NULL) OR (source_frame_count >= 0))),
    CONSTRAINT ck_calculation_segment_wall_time_nonnegative CHECK (((wall_time_seconds IS NULL) OR (wall_time_seconds >= (0)::double precision)))
);


--
-- Name: calculation_status_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.calculation_status_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    scf_converged boolean,
    normal_terminated boolean,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_calculation_status_result_has_value CHECK ((num_nonnulls(scf_converged, normal_terminated) > 0))
);


--
-- Name: charge_spin_population_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.charge_spin_population_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    series_count integer NOT NULL,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_charge_spin_population_result_count_nonnegative CHECK ((series_count >= 0))
);


--
-- Name: electronic_configuration; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.electronic_configuration (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    electronic_state_id uuid NOT NULL,
    configuration_ordinal integer NOT NULL,
    label character varying(256),
    coefficient double precision,
    weight double precision,
    occupation double precision[] NOT NULL,
    orbital_indices integer[] NOT NULL,
    raw text NOT NULL,
    CONSTRAINT ck_electronic_configuration_ordinal_nonnegative CHECK ((configuration_ordinal >= 0))
);


--
-- Name: electronic_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.electronic_state (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    state_set_id uuid NOT NULL,
    state_ordinal integer NOT NULL,
    state_index integer,
    root integer,
    label character varying(256),
    multiplicity integer,
    spin double precision,
    irrep character varying(128),
    method character varying(128),
    energy_hartree numeric(24,6),
    excitation_energy_ev double precision,
    oscillator_strength double precision,
    properties jsonb NOT NULL,
    source text,
    CONSTRAINT ck_electronic_state_multiplicity_positive CHECK (((multiplicity IS NULL) OR (multiplicity > 0))),
    CONSTRAINT ck_electronic_state_ordinal_nonnegative CHECK ((state_ordinal >= 0))
);


--
-- Name: electronic_state_set; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.electronic_state_set (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    kind character varying(14) NOT NULL,
    state_count integer NOT NULL,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_electronic_state_set_count_nonnegative CHECK ((state_count >= 0)),
    CONSTRAINT electronic_state_set_kind CHECK (((kind)::text = ANY ((ARRAY['frame'::character varying, 'multireference'::character varying])::text[])))
);


--
-- Name: energy_observation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.energy_observation (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    energy_result_id uuid NOT NULL,
    observation_index smallint NOT NULL,
    method character varying(128) NOT NULL,
    quantity_semantics character varying(22) NOT NULL,
    value_hartree numeric(24,6) NOT NULL,
    source_label character varying(256) NOT NULL,
    CONSTRAINT ck_energy_observation_index_nonnegative CHECK ((observation_index >= 0)),
    CONSTRAINT energy_observation_quantity_semantics CHECK (((quantity_semantics)::text = ANY ((ARRAY['total_energy'::character varying, 'correlation_correction'::character varying, 'component'::character varying])::text[])))
);


--
-- Name: external_identity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_identity (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    issuer character varying(512) NOT NULL,
    subject character varying(512) NOT NULL,
    email character varying(320),
    claims jsonb NOT NULL,
    last_authenticated_at timestamp with time zone
);


--
-- Name: frame_energy_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.frame_energy_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    electronic_energy_hartree numeric(24,6),
    reference_energy_hartree numeric(24,6),
    mp2_energy_hartree numeric(24,6),
    mp3_energy_hartree numeric(24,6),
    mp4_energy_hartree numeric(24,6),
    mp5_energy_hartree numeric(24,6),
    ccsd_energy_hartree numeric(24,6),
    ccsd_t_energy_hartree numeric(24,6),
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_frame_energy_result_has_value CHECK ((num_nonnulls(electronic_energy_hartree, reference_energy_hartree, mp2_energy_hartree, mp3_energy_hartree, mp4_energy_hartree, mp5_energy_hartree, ccsd_energy_hartree, ccsd_t_energy_hartree) > 0))
);


--
-- Name: geometry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.geometry (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    topology_id uuid NOT NULL,
    geometry_hash character varying(64) NOT NULL,
    canonicalization_version character varying(64) DEFAULT 'geometry-internal-coordinates-v1'::character varying NOT NULL,
    mol public.mol NOT NULL,
    internal_coordinates bytea NOT NULL,
    internal_coordinate_hash character varying(64) NOT NULL,
    internal_coordinate_distances_angstrom double precision[] NOT NULL,
    internal_coordinate_angles_degrees double precision[] NOT NULL,
    internal_coordinate_dihedrals_degrees double precision[] NOT NULL,
    minimum_coordinate_decimal_places smallint,
    CONSTRAINT ck_geometry_hash_hex CHECK (((geometry_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_geometry_internal_coordinate_hash_hex CHECK (((internal_coordinate_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_geometry_internal_coordinate_match_projection CHECK (((cardinality(internal_coordinate_distances_angstrom) > 0) AND (cardinality(internal_coordinate_distances_angstrom) = cardinality(internal_coordinate_angles_degrees)) AND (cardinality(internal_coordinate_distances_angstrom) = cardinality(internal_coordinate_dihedrals_degrees)) AND (array_position(internal_coordinate_distances_angstrom, NULL::double precision) IS NULL) AND (array_position(internal_coordinate_angles_degrees, NULL::double precision) IS NULL) AND (array_position(internal_coordinate_dihedrals_degrees, NULL::double precision) IS NULL))),
    CONSTRAINT ck_geometry_minimum_coordinate_decimal_places CHECK (((minimum_coordinate_decimal_places IS NULL) OR ((minimum_coordinate_decimal_places >= 0) AND (minimum_coordinate_decimal_places <= 18))))
);


--
-- Name: geometry_optimization_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.geometry_optimization_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    geometry_optimized boolean,
    convergence_multiplier double precision NOT NULL,
    source_converged jsonb,
    source_labels jsonb,
    energy_change_hartree double precision,
    energy_change_threshold_hartree double precision,
    energy_change_converged boolean,
    rms_force_hartree_per_bohr double precision,
    rms_force_threshold_hartree_per_bohr double precision,
    rms_force_converged boolean,
    max_force_hartree_per_bohr double precision,
    max_force_threshold_hartree_per_bohr double precision,
    max_force_converged boolean,
    rms_displacement_bohr double precision,
    rms_displacement_threshold_bohr double precision,
    rms_displacement_converged boolean,
    max_displacement_bohr double precision,
    max_displacement_threshold_bohr double precision,
    max_displacement_converged boolean,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_geometry_optimization_result_multiplier CHECK ((convergence_multiplier >= (1)::double precision))
);


--
-- Name: implicit_solvation_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.implicit_solvation_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    solvent character varying(128),
    solvent_model character varying(128),
    atomic_radii character varying(128),
    solvent_epsilon double precision,
    solvent_epsilon_infinite double precision,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_implicit_solvation_result_epsilon_infinite_positive CHECK (((solvent_epsilon_infinite IS NULL) OR (solvent_epsilon_infinite > (0)::double precision))),
    CONSTRAINT ck_implicit_solvation_result_epsilon_positive CHECK (((solvent_epsilon IS NULL) OR (solvent_epsilon > (0)::double precision)))
);


--
-- Name: logical_reaction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.logical_reaction (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reaction_key text NOT NULL,
    label text,
    reaction_class character varying(13),
    cycloaddition_pattern character varying(32),
    reaction_hash character varying(64) NOT NULL,
    CONSTRAINT ck_reaction_hash_hex CHECK (((reaction_hash)::text ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: logical_reaction_participant; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.logical_reaction_participant (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    logical_reaction_id uuid NOT NULL,
    topology_id uuid NOT NULL,
    side character varying(8) NOT NULL,
    participant_index smallint NOT NULL,
    role character varying(13),
    stoichiometric_coefficient smallint CONSTRAINT logical_reaction_participan_stoichiometric_coefficient_not_null NOT NULL,
    CONSTRAINT ck_logical_participant_index CHECK ((participant_index >= 0)),
    CONSTRAINT ck_logical_participant_stoichiometry CHECK ((stoichiometric_coefficient > 0)),
    CONSTRAINT logical_reaction_participant_role CHECK (((role)::text = ANY ((ARRAY['diene'::character varying, 'dienophile'::character varying, 'dipole'::character varying, 'dipolarophile'::character varying, 'product'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT logical_reaction_participant_side CHECK (((side)::text = ANY ((ARRAY['reactant'::character varying, 'product'::character varying])::text[])))
);


--
-- Name: manifest_artifact_binding; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.manifest_artifact_binding (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    workflow_manifest_id uuid NOT NULL,
    artifact_key text NOT NULL,
    artifact_file_id uuid,
    expected_content_sha256 character varying(64),
    artifact_role character varying(17) NOT NULL,
    reaction_key text NOT NULL,
    path_key text NOT NULL,
    node_key text NOT NULL,
    segment_index integer,
    frame_index integer,
    source_geometry_artifact_key text,
    resolution_status character varying(13) DEFAULT 'declared'::character varying NOT NULL,
    CONSTRAINT ck_manifest_artifact_binding_expected_hash_hex CHECK (((expected_content_sha256 IS NULL) OR ((expected_content_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_manifest_artifact_binding_frame_nonnegative CHECK (((frame_index IS NULL) OR (frame_index >= 0))),
    CONSTRAINT ck_manifest_artifact_binding_not_self_sourcing CHECK (((source_geometry_artifact_key IS NULL) OR (source_geometry_artifact_key <> artifact_key))),
    CONSTRAINT ck_manifest_artifact_binding_resolved_payload CHECK ((((resolution_status)::text <> 'resolved'::text) OR ((artifact_file_id IS NOT NULL) AND (expected_content_sha256 IS NOT NULL) AND (segment_index IS NOT NULL) AND (frame_index IS NOT NULL)))),
    CONSTRAINT ck_manifest_artifact_binding_segment_nonnegative CHECK (((segment_index IS NULL) OR (segment_index >= 0))),
    CONSTRAINT ck_manifest_artifact_binding_selector_complete CHECK ((num_nonnulls(segment_index, frame_index) = ANY (ARRAY[0, 2]))),
    CONSTRAINT manifest_artifact_binding_resolution_status CHECK (((resolution_status)::text = ANY ((ARRAY['declared'::character varying, 'resolved'::character varying, 'missing'::character varying, 'hash_mismatch'::character varying, 'parse_failed'::character varying, 'quarantined'::character varying])::text[]))),
    CONSTRAINT manifest_artifact_binding_role CHECK (((artifact_role)::text = ANY ((ARRAY['gaussian_opt_freq'::character varying, 'orca_single_point'::character varying, 'input'::character varying, 'supporting'::character varying])::text[])))
);


--
-- Name: mapped_reaction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mapped_reaction (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    logical_reaction_id uuid NOT NULL,
    mapped_reaction_key text NOT NULL,
    label text,
    mapped_reaction_kind character varying(14) NOT NULL,
    mapped_reaction_smiles text NOT NULL,
    mapping_hash character varying(64) NOT NULL,
    reaction public.reaction GENERATED ALWAYS AS (public.reaction_from_smiles((mapped_reaction_smiles)::cstring)) STORED NOT NULL,
    reaction_structural_bfp public.bfp GENERATED ALWAYS AS (public.reaction_structural_bfp(public.reaction_from_smiles((mapped_reaction_smiles)::cstring), 5)) STORED NOT NULL,
    reaction_structural_bfp_schema_version text DEFAULT 'reaction-structural-bfp-r5-v1'::text NOT NULL,
    thermodynamic_profile_policy_version text,
    minimum_activation_gibbs_free_energy_kcal_mol double precision,
    maximum_activation_gibbs_free_energy_kcal_mol double precision,
    minimum_reaction_gibbs_free_energy_kcal_mol double precision,
    maximum_reaction_gibbs_free_energy_kcal_mol double precision,
    CONSTRAINT ck_mapped_reaction_structural_bfp_schema_version CHECK ((reaction_structural_bfp_schema_version = 'reaction-structural-bfp-r5-v1'::text)),
    CONSTRAINT ck_mapping_hash_hex CHECK (((mapping_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT mapped_reaction_kind CHECK (((mapped_reaction_kind)::text = ANY ((ARRAY['curated'::character varying, 'minimum_energy'::character varying, 'irc_supported'::character varying, 'other'::character varying])::text[])))
);


--
-- Name: mapped_reaction_edge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mapped_reaction_edge (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    mapped_reaction_id uuid NOT NULL,
    edge_key text NOT NULL,
    source_node_id uuid NOT NULL,
    target_node_id uuid NOT NULL,
    transition_state_node_id uuid,
    edge_kind character varying(15) NOT NULL,
    CONSTRAINT ck_mapped_reaction_edge_distinct_endpoints CHECK ((source_node_id <> target_node_id)),
    CONSTRAINT mapped_reaction_edge_kind CHECK (((edge_kind)::text = ANY ((ARRAY['elementary_step'::character varying, 'conformational'::character varying, 'association'::character varying, 'dissociation'::character varying, 'other'::character varying])::text[])))
);


--
-- Name: mapped_reaction_node; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mapped_reaction_node (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    mapped_reaction_id uuid NOT NULL,
    node_key text NOT NULL,
    node_index integer NOT NULL,
    role character varying(16) NOT NULL,
    CONSTRAINT ck_mapped_reaction_node_index_nonnegative CHECK ((node_index >= 0)),
    CONSTRAINT mapped_reaction_node_role CHECK (((role)::text = ANY ((ARRAY['reactant'::character varying, 'reactant_complex'::character varying, 'intermediate'::character varying, 'transition_state'::character varying, 'product'::character varying, 'product_complex'::character varying, 'other'::character varying])::text[])))
);


--
-- Name: mapped_reaction_node_geometry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mapped_reaction_node_geometry (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    mapped_reaction_node_id uuid NOT NULL,
    geometry_id uuid NOT NULL,
    mapped_reaction_participant_id uuid,
    component_key text NOT NULL,
    component_index smallint NOT NULL,
    coordinate_index smallint NOT NULL,
    is_primary boolean NOT NULL,
    CONSTRAINT ck_node_geometry_indices_nonnegative CHECK (((component_index >= 0) AND (coordinate_index >= 0)))
);


--
-- Name: mapped_reaction_node_geometry_mapping; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mapped_reaction_node_geometry_mapping (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    mapped_reaction_node_geometry_id uuid CONSTRAINT mapped_reaction_node_geome_mapped_reaction_node_geome_not_null1 NOT NULL,
    geometry_atom_map_numbers integer[] CONSTRAINT mapped_reaction_node_geometr_topology_atom_map_numbers_not_null NOT NULL,
    mapped_smiles text NOT NULL,
    mapping_method character varying(64) NOT NULL,
    mapping_version character varying(64) NOT NULL,
    verified boolean NOT NULL,
    CONSTRAINT ck_node_geometry_mapping_atom_maps_valid CHECK (((cardinality(geometry_atom_map_numbers) > 0) AND (array_position(geometry_atom_map_numbers, NULL::integer) IS NULL) AND (0 < ALL (geometry_atom_map_numbers))))
);


--
-- Name: mapped_reaction_participant; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mapped_reaction_participant (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    mapped_reaction_id uuid NOT NULL,
    logical_reaction_participant_id uuid CONSTRAINT mapped_reaction_participant_logical_reaction_participa_not_null NOT NULL,
    side character varying(8) NOT NULL,
    template_index smallint NOT NULL,
    atom_map_numbers integer[] NOT NULL,
    mapped_smiles text NOT NULL,
    CONSTRAINT ck_mapped_participant_atom_maps CHECK (((cardinality(atom_map_numbers) > 0) AND (array_position(atom_map_numbers, NULL::integer) IS NULL) AND (0 < ALL (atom_map_numbers)))),
    CONSTRAINT ck_mapped_participant_template_index CHECK ((template_index >= 0)),
    CONSTRAINT mapped_reaction_participant_side CHECK (((side)::text = ANY ((ARRAY['reactant'::character varying, 'product'::character varying])::text[])))
);


--
-- Name: mapped_reaction_thermodynamic_profile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mapped_reaction_thermodynamic_profile (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    mapped_reaction_id uuid CONSTRAINT mapped_reaction_thermodynamic_profi_mapped_reaction_id_not_null NOT NULL,
    policy_version text NOT NULL,
    source_key_hash character varying(64) NOT NULL,
    electronic_level jsonb NOT NULL,
    thermochemistry_level jsonb CONSTRAINT mapped_reaction_thermodynamic_pr_thermochemistry_level_not_null NOT NULL,
    temperature_kelvin double precision CONSTRAINT mapped_reaction_thermodynamic_profi_temperature_kelvin_not_null NOT NULL,
    pressure_atm double precision NOT NULL,
    reactants jsonb NOT NULL,
    transition_state jsonb,
    products jsonb,
    reactants_enthalpy_hartree double precision CONSTRAINT mapped_reaction_thermodynam_reactants_enthalpy_hartree_not_null NOT NULL,
    reactants_gibbs_free_energy_hartree double precision CONSTRAINT mapped_reaction_thermodynam_reactants_gibbs_free_energ_not_null NOT NULL,
    reactants_entropy_cal_mol_k double precision CONSTRAINT mapped_reaction_thermodynam_reactants_entropy_cal_mol__not_null NOT NULL,
    transition_state_enthalpy_hartree double precision,
    transition_state_gibbs_free_energy_hartree double precision,
    transition_state_entropy_cal_mol_k double precision,
    products_enthalpy_hartree double precision,
    products_gibbs_free_energy_hartree double precision,
    products_entropy_cal_mol_k double precision,
    activation_enthalpy_kcal_mol double precision GENERATED ALWAYS AS (((transition_state_enthalpy_hartree - reactants_enthalpy_hartree) * (627.5094740631)::double precision)) STORED,
    activation_gibbs_free_energy_kcal_mol double precision GENERATED ALWAYS AS (((transition_state_gibbs_free_energy_hartree - reactants_gibbs_free_energy_hartree) * (627.5094740631)::double precision)) STORED,
    activation_entropy_cal_mol_k double precision GENERATED ALWAYS AS ((transition_state_entropy_cal_mol_k - reactants_entropy_cal_mol_k)) STORED,
    reaction_enthalpy_kcal_mol double precision GENERATED ALWAYS AS (((products_enthalpy_hartree - reactants_enthalpy_hartree) * (627.5094740631)::double precision)) STORED,
    reaction_gibbs_free_energy_kcal_mol double precision GENERATED ALWAYS AS (((products_gibbs_free_energy_hartree - reactants_gibbs_free_energy_hartree) * (627.5094740631)::double precision)) STORED,
    reaction_entropy_cal_mol_k double precision GENERATED ALWAYS AS ((products_entropy_cal_mol_k - reactants_entropy_cal_mol_k)) STORED
);


--
-- Name: mcp_access_token; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_access_token (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    name character varying(128) NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);


--
-- Name: molecular_formula; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.molecular_formula (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    hill_formula text NOT NULL,
    composition jsonb NOT NULL,
    composition_schema_version character varying(64) DEFAULT 'formula-composition-v1'::character varying NOT NULL,
    atom_count integer NOT NULL,
    composition_hash character varying(64) NOT NULL,
    element_count_vector integer[] NOT NULL,
    element_count_vector_schema_version character varying(64) DEFAULT 'atomic-number-count-v1'::character varying NOT NULL,
    element_count_tokens text[] GENERATED ALWAYS AS (ARRAY[('1:'::text || (element_count_vector[1])::text), ('2:'::text || (element_count_vector[2])::text), ('3:'::text || (element_count_vector[3])::text), ('4:'::text || (element_count_vector[4])::text), ('5:'::text || (element_count_vector[5])::text), ('6:'::text || (element_count_vector[6])::text), ('7:'::text || (element_count_vector[7])::text), ('8:'::text || (element_count_vector[8])::text), ('9:'::text || (element_count_vector[9])::text), ('10:'::text || (element_count_vector[10])::text), ('11:'::text || (element_count_vector[11])::text), ('12:'::text || (element_count_vector[12])::text), ('13:'::text || (element_count_vector[13])::text), ('14:'::text || (element_count_vector[14])::text), ('15:'::text || (element_count_vector[15])::text), ('16:'::text || (element_count_vector[16])::text), ('17:'::text || (element_count_vector[17])::text), ('18:'::text || (element_count_vector[18])::text), ('19:'::text || (element_count_vector[19])::text), ('20:'::text || (element_count_vector[20])::text), ('21:'::text || (element_count_vector[21])::text), ('22:'::text || (element_count_vector[22])::text), ('23:'::text || (element_count_vector[23])::text), ('24:'::text || (element_count_vector[24])::text), ('25:'::text || (element_count_vector[25])::text), ('26:'::text || (element_count_vector[26])::text), ('27:'::text || (element_count_vector[27])::text), ('28:'::text || (element_count_vector[28])::text), ('29:'::text || (element_count_vector[29])::text), ('30:'::text || (element_count_vector[30])::text), ('31:'::text || (element_count_vector[31])::text), ('32:'::text || (element_count_vector[32])::text), ('33:'::text || (element_count_vector[33])::text), ('34:'::text || (element_count_vector[34])::text), ('35:'::text || (element_count_vector[35])::text), ('36:'::text || (element_count_vector[36])::text), ('37:'::text || (element_count_vector[37])::text), ('38:'::text || (element_count_vector[38])::text), ('39:'::text || (element_count_vector[39])::text), ('40:'::text || (element_count_vector[40])::text), ('41:'::text || (element_count_vector[41])::text), ('42:'::text || (element_count_vector[42])::text), ('43:'::text || (element_count_vector[43])::text), ('44:'::text || (element_count_vector[44])::text), ('45:'::text || (element_count_vector[45])::text), ('46:'::text || (element_count_vector[46])::text), ('47:'::text || (element_count_vector[47])::text), ('48:'::text || (element_count_vector[48])::text), ('49:'::text || (element_count_vector[49])::text), ('50:'::text || (element_count_vector[50])::text), ('51:'::text || (element_count_vector[51])::text), ('52:'::text || (element_count_vector[52])::text), ('53:'::text || (element_count_vector[53])::text), ('54:'::text || (element_count_vector[54])::text), ('55:'::text || (element_count_vector[55])::text), ('56:'::text || (element_count_vector[56])::text), ('57:'::text || (element_count_vector[57])::text), ('58:'::text || (element_count_vector[58])::text), ('59:'::text || (element_count_vector[59])::text), ('60:'::text || (element_count_vector[60])::text), ('61:'::text || (element_count_vector[61])::text), ('62:'::text || (element_count_vector[62])::text), ('63:'::text || (element_count_vector[63])::text), ('64:'::text || (element_count_vector[64])::text), ('65:'::text || (element_count_vector[65])::text), ('66:'::text || (element_count_vector[66])::text), ('67:'::text || (element_count_vector[67])::text), ('68:'::text || (element_count_vector[68])::text), ('69:'::text || (element_count_vector[69])::text), ('70:'::text || (element_count_vector[70])::text), ('71:'::text || (element_count_vector[71])::text), ('72:'::text || (element_count_vector[72])::text), ('73:'::text || (element_count_vector[73])::text), ('74:'::text || (element_count_vector[74])::text), ('75:'::text || (element_count_vector[75])::text), ('76:'::text || (element_count_vector[76])::text), ('77:'::text || (element_count_vector[77])::text), ('78:'::text || (element_count_vector[78])::text), ('79:'::text || (element_count_vector[79])::text), ('80:'::text || (element_count_vector[80])::text), ('81:'::text || (element_count_vector[81])::text), ('82:'::text || (element_count_vector[82])::text), ('83:'::text || (element_count_vector[83])::text), ('84:'::text || (element_count_vector[84])::text), ('85:'::text || (element_count_vector[85])::text), ('86:'::text || (element_count_vector[86])::text), ('87:'::text || (element_count_vector[87])::text), ('88:'::text || (element_count_vector[88])::text), ('89:'::text || (element_count_vector[89])::text), ('90:'::text || (element_count_vector[90])::text), ('91:'::text || (element_count_vector[91])::text), ('92:'::text || (element_count_vector[92])::text), ('93:'::text || (element_count_vector[93])::text), ('94:'::text || (element_count_vector[94])::text), ('95:'::text || (element_count_vector[95])::text), ('96:'::text || (element_count_vector[96])::text), ('97:'::text || (element_count_vector[97])::text), ('98:'::text || (element_count_vector[98])::text), ('99:'::text || (element_count_vector[99])::text), ('100:'::text || (element_count_vector[100])::text), ('101:'::text || (element_count_vector[101])::text), ('102:'::text || (element_count_vector[102])::text), ('103:'::text || (element_count_vector[103])::text), ('104:'::text || (element_count_vector[104])::text), ('105:'::text || (element_count_vector[105])::text), ('106:'::text || (element_count_vector[106])::text), ('107:'::text || (element_count_vector[107])::text), ('108:'::text || (element_count_vector[108])::text), ('109:'::text || (element_count_vector[109])::text), ('110:'::text || (element_count_vector[110])::text), ('111:'::text || (element_count_vector[111])::text), ('112:'::text || (element_count_vector[112])::text), ('113:'::text || (element_count_vector[113])::text), ('114:'::text || (element_count_vector[114])::text), ('115:'::text || (element_count_vector[115])::text), ('116:'::text || (element_count_vector[116])::text), ('117:'::text || (element_count_vector[117])::text), ('118:'::text || (element_count_vector[118])::text)]) STORED NOT NULL,
    CONSTRAINT ck_molecular_formula_atom_count_positive CHECK ((atom_count > 0)),
    CONSTRAINT ck_molecular_formula_composition_hash_hex CHECK (((composition_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_molecular_formula_element_count_vector_length CHECK ((cardinality(element_count_vector) = 118)),
    CONSTRAINT ck_molecular_formula_element_count_vector_nonnegative CHECK ((0 <= ALL (element_count_vector)))
);


--
-- Name: molecular_orbital_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.molecular_orbital_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    electronic_state character varying(128),
    alpha_orbital_count integer NOT NULL,
    beta_orbital_count integer NOT NULL,
    coefficient_count integer NOT NULL,
    alpha_occupancies double precision[] NOT NULL,
    beta_occupancies double precision[] NOT NULL,
    alpha_symmetries character varying(128)[] NOT NULL,
    beta_symmetries character varying(128)[] NOT NULL,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_molecular_orbital_result_counts_nonnegative CHECK (((alpha_orbital_count >= 0) AND (beta_orbital_count >= 0) AND (coefficient_count >= 0)))
);


--
-- Name: molecular_topology; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.molecular_topology (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    formula_id uuid NOT NULL,
    mol public.mol NOT NULL,
    canonical_isomeric_smiles text,
    graph_hash character varying(64) NOT NULL,
    identity_schema_version character varying(64) DEFAULT 'topology-identity-v1'::character varying NOT NULL,
    atom_count integer NOT NULL,
    heavy_atom_count integer NOT NULL,
    formal_charge smallint NOT NULL,
    radical_electron_count smallint NOT NULL,
    fragment_count smallint NOT NULL,
    stereo_status character varying(10) DEFAULT 'unknown'::character varying NOT NULL,
    morgan_bfp_schema_version character varying(64) DEFAULT 'morgan-bfp-r2-v1'::character varying NOT NULL,
    sanitization_status character varying(16) DEFAULT 'sanitized'::character varying NOT NULL,
    sanitization_error text,
    morgan_bfp public.bfp GENERATED ALWAYS AS (
CASE
    WHEN ((sanitization_status)::text = 'sanitized'::text) THEN public.morganbv_fp(public.mol_from_smiles(canonical_isomeric_smiles), 2)
    ELSE NULL::public.bfp
END) STORED,
    CONSTRAINT ck_molecular_topology_atom_count_positive CHECK ((atom_count > 0)),
    CONSTRAINT ck_molecular_topology_fragment_count CHECK ((fragment_count > 0)),
    CONSTRAINT ck_molecular_topology_graph_hash_hex CHECK (((graph_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_molecular_topology_heavy_atoms CHECK ((heavy_atom_count >= 0)),
    CONSTRAINT ck_molecular_topology_morgan_bfp_schema_version CHECK (((morgan_bfp_schema_version)::text = 'morgan-bfp-r2-v1'::text)),
    CONSTRAINT ck_molecular_topology_radical_electrons CHECK ((radical_electron_count >= 0)),
    CONSTRAINT ck_molecular_topology_sanitization_evidence CHECK (((((sanitization_status)::text = 'sanitized'::text) AND (sanitization_error IS NULL) AND (canonical_isomeric_smiles IS NOT NULL)) OR (((sanitization_status)::text = 'failed'::text) AND (sanitization_error IS NOT NULL)))),
    CONSTRAINT molecular_topology_sanitization_status CHECK (((sanitization_status)::text = ANY ((ARRAY['sanitized'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT molecular_topology_stereo_status CHECK (((stereo_status)::text = ANY ((ARRAY['assigned'::character varying, 'unassigned'::character varying, 'unknown'::character varying, 'conflict'::character varying])::text[])))
);


--
-- Name: molecular_topology_derivation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.molecular_topology_derivation (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    topology_id uuid NOT NULL,
    reconstruction_method character varying(128) NOT NULL,
    reconstruction_version character varying(128) NOT NULL,
    reconstruction_metadata jsonb NOT NULL,
    provenance_schema_version character varying(64) DEFAULT 'topology-derivation-v1'::character varying CONSTRAINT molecular_topology_derivatio_provenance_schema_version_not_null NOT NULL,
    provenance_hash character varying(64) NOT NULL,
    CONSTRAINT ck_molecular_topology_derivation_hash_hex CHECK (((provenance_hash)::text ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: multireference_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.multireference_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    electronic_state_set_id uuid,
    method character varying(128),
    reference_method character varying(128),
    ci_type character varying(128),
    active_space_electrons integer,
    active_space_orbitals integer,
    active_space_roots integer,
    active_orbitals integer[] NOT NULL,
    inactive_orbitals integer[] NOT NULL,
    frozen_orbitals integer[] NOT NULL,
    active_space_raw text NOT NULL,
    active_space_options jsonb NOT NULL,
    corrections jsonb NOT NULL,
    diagnostics text[] NOT NULL,
    properties jsonb NOT NULL,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_multireference_result_active_space_nonnegative CHECK ((((active_space_electrons IS NULL) OR (active_space_electrons >= 0)) AND ((active_space_orbitals IS NULL) OR (active_space_orbitals >= 0)) AND ((active_space_roots IS NULL) OR (active_space_roots >= 0))))
);


--
-- Name: nmr_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nmr_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    gauge character varying(128),
    shielding_count integer NOT NULL,
    coupling_atom_indices integer[] NOT NULL,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_nmr_result_shielding_count_nonnegative CHECK ((shielding_count >= 0))
);


--
-- Name: nmr_shielding_tensor; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nmr_shielding_tensor (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    result_id uuid NOT NULL,
    atom_index integer NOT NULL,
    atom_symbol character varying(8) NOT NULL,
    isotropic_ppm double precision,
    anisotropy_ppm double precision,
    anisotropy_convention character varying(64),
    orientation character varying(16) NOT NULL,
    CONSTRAINT ck_nmr_shielding_tensor_atom_index_nonnegative CHECK ((atom_index >= 0)),
    CONSTRAINT ck_nmr_shielding_tensor_orientation CHECK (((orientation)::text = ANY ((ARRAY['input'::character varying, 'standard'::character varying, 'source'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: organization; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organization (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    slug character varying(128) NOT NULL,
    name text NOT NULL,
    status character varying(9) DEFAULT 'active'::character varying NOT NULL,
    CONSTRAINT ck_organization_slug_format CHECK (((slug)::text ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'::text)),
    CONSTRAINT organization_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'suspended'::character varying])::text[])))
);


--
-- Name: organization_membership; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organization_membership (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role character varying(6) NOT NULL,
    CONSTRAINT organization_membership_role CHECK (((role)::text = ANY ((ARRAY['owner'::character varying, 'admin'::character varying, 'member'::character varying])::text[])))
);


--
-- Name: parse_revision; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parse_revision (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    artifact_file_id uuid NOT NULL,
    export_schema_version character varying(64) NOT NULL,
    parser_name character varying(64) DEFAULT 'molop'::character varying NOT NULL,
    parser_version character varying(128) NOT NULL,
    parser_commit character varying(128),
    molgr_version character varying(128),
    molgr_commit character varying(128),
    rdkit_version character varying(128) NOT NULL,
    parser_provenance_hash character varying(64) NOT NULL,
    parser_config_hash character varying(64) NOT NULL,
    reconstruction_config_hash character varying(64) NOT NULL,
    source_format character varying(12) NOT NULL,
    source_encoding character varying(64) NOT NULL,
    record_sha256 character varying(64),
    status character varying(11) DEFAULT 'pending'::character varying NOT NULL,
    error_code character varying(128),
    error_message text,
    error_metadata jsonb,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    source_complete boolean,
    parse_completeness character varying(12) DEFAULT 'not_assessed'::character varying NOT NULL,
    parse_diagnostics jsonb DEFAULT '[]'::jsonb NOT NULL,
    parser_id character varying(512) NOT NULL,
    molop_version character varying(128) NOT NULL,
    parser_provenance jsonb NOT NULL,
    source_content_sha256 character varying(64),
    source_size_bytes bigint,
    source_compression character varying(32),
    revision_number integer NOT NULL,
    reparse_of_id uuid,
    CONSTRAINT ck_parse_revision_number_positive CHECK ((revision_number >= 1)),
    CONSTRAINT ck_parse_revision_parser_config_hash_hex CHECK (((parser_config_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_parse_revision_provenance_hash_hex CHECK (((parser_provenance_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_parse_revision_reconstruction_config_hash_hex CHECK (((reconstruction_config_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_parse_revision_record_hash_hex CHECK (((record_sha256 IS NULL) OR ((record_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_parse_revision_source_hash_hex CHECK (((source_content_sha256 IS NULL) OR ((source_content_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_parse_revision_source_size_nonnegative CHECK (((source_size_bytes IS NULL) OR (source_size_bytes >= 0))),
    CONSTRAINT ck_parse_revision_succeeded_payload CHECK ((((status)::text <> 'succeeded'::text) OR ((record_sha256 IS NOT NULL) AND (completed_at IS NOT NULL)))),
    CONSTRAINT ck_parse_revision_timestamps_ordered CHECK (((completed_at IS NULL) OR (started_at IS NULL) OR (completed_at >= started_at))),
    CONSTRAINT parse_revision_parse_completeness CHECK (((parse_completeness)::text = ANY ((ARRAY['not_assessed'::character varying, 'complete'::character varying, 'partial'::character varying])::text[]))),
    CONSTRAINT parse_revision_source_format CHECK (((source_format)::text = ANY ((ARRAY['gaussian_log'::character varying, 'orca_output'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT parse_revision_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'succeeded'::character varying, 'quarantined'::character varying, 'failed'::character varying])::text[])))
);


--
-- Name: polarizability_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.polarizability_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    electronic_spatial_extent_bohr2 double precision,
    isotropic_polarizability_bohr3 double precision,
    anisotropic_polarizability_bohr3 double precision,
    source_schema_version character varying(64) NOT NULL
);


--
-- Name: project; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    slug character varying(128) NOT NULL,
    name text NOT NULL,
    status character varying(8) DEFAULT 'active'::character varying NOT NULL,
    CONSTRAINT ck_project_slug_format CHECK (((slug)::text ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'::text)),
    CONSTRAINT project_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'archived'::character varying])::text[])))
);


--
-- Name: project_invitation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_invitation (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    invited_by_user_id uuid NOT NULL,
    email character varying(320) NOT NULL,
    role character varying(11) NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    accepted_at timestamp with time zone,
    revoked_at timestamp with time zone,
    delivery_status character varying(32) DEFAULT 'link_only'::character varying NOT NULL,
    delivery_error text,
    delivery_sent_at timestamp with time zone,
    CONSTRAINT project_invitation_role CHECK (((role)::text = ANY ((ARRAY['manager'::character varying, 'contributor'::character varying, 'viewer'::character varying])::text[])))
);


--
-- Name: project_membership; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_membership (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role character varying(11) NOT NULL,
    CONSTRAINT project_membership_role CHECK (((role)::text = ANY ((ARRAY['manager'::character varying, 'contributor'::character varying, 'viewer'::character varying])::text[])))
);


--
-- Name: scientific_array; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scientific_array (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    kind character varying(29) NOT NULL,
    ordinal smallint NOT NULL,
    unit character varying(64) NOT NULL,
    dtype character varying(64) NOT NULL,
    shape integer[] NOT NULL,
    array_nbytes bigint NOT NULL,
    payload_sha256 character varying(64) NOT NULL,
    data bytea NOT NULL,
    metadata_schema_version character varying(64),
    metadata jsonb,
    CONSTRAINT ck_scientific_array_kind_unit CHECK (((((kind)::text = 'forces'::text) AND ((unit)::text = 'hartree/bohr'::text)) OR (((kind)::text = 'hessian'::text) AND ((unit)::text = 'hartree/bohr^2'::text)) OR (((kind)::text = 'vibrational_frequencies'::text) AND ((unit)::text = 'cm^-1'::text)) OR (((kind)::text = 'reduced_masses'::text) AND ((unit)::text = 'amu'::text)) OR (((kind)::text = 'vibrational_force_constants'::text) AND ((unit)::text = 'mdyne/angstrom'::text)) OR (((kind)::text = 'ir_intensities'::text) AND ((unit)::text = 'km/mol'::text)) OR (((kind)::text = 'normal_modes'::text) AND ((unit)::text = 'angstrom'::text)) OR (((kind)::text = 'moments_of_inertia'::text) AND ((unit)::text = 'amu*bohr^2'::text)) OR (((kind)::text = 'rotational_temperatures'::text) AND ((unit)::text = 'kelvin'::text)) OR (((kind)::text = 'rotational_constants'::text) AND ((unit)::text = 'gigahertz'::text)) OR (((kind)::text = 'vibrational_temperatures'::text) AND ((unit)::text = 'kelvin'::text)) OR (((kind)::text = ANY (ARRAY[('orbital_alpha_energies'::character varying)::text, ('orbital_beta_energies'::character varying)::text])) AND ((unit)::text = 'hartree'::text)) OR (((kind)::text = ANY (ARRAY[('orbital_coefficient'::character varying)::text, ('atomic_population'::character varying)::text, ('bond_order_matrix'::character varying)::text, ('fukui_positive'::character varying)::text, ('fukui_negative'::character varying)::text, ('fukui_zero'::character varying)::text, ('fractional_occupation_density'::character varying)::text])) AND ((unit)::text = 'dimensionless'::text)) OR (((kind)::text = 'polarizability_tensor'::text) AND ((unit)::text = 'bohr^3'::text)) OR (((kind)::text = ANY (ARRAY[('electric_dipole_moment'::character varying)::text, ('dipole'::character varying)::text, ('transition_dipole'::character varying)::text])) AND ((unit)::text = 'debye'::text)) OR (((kind)::text = ANY (ARRAY[('quadrupole'::character varying)::text, ('traceless_quadrupole'::character varying)::text])) AND ((unit)::text = 'debye*angstrom'::text)) OR (((kind)::text = 'octapole'::text) AND ((unit)::text = 'debye*angstrom^2'::text)) OR (((kind)::text = 'hexadecapole'::text) AND ((unit)::text = 'debye*angstrom^3'::text)) OR (((kind)::text = ANY (ARRAY[('nmr_shielding_tensor'::character varying)::text, ('nmr_principal_values'::character varying)::text])) AND ((unit)::text = 'ppm'::text)) OR (((kind)::text = ANY (ARRAY[('nmr_coupling_k'::character varying)::text, ('nmr_coupling_j'::character varying)::text, ('nmr_coupling_k_component'::character varying)::text, ('nmr_coupling_j_component'::character varying)::text])) AND ((unit)::text = 'hertz'::text)))),
    CONSTRAINT ck_scientific_array_metadata_complete CHECK ((num_nonnulls(metadata_schema_version, metadata) = ANY (ARRAY[0, 2]))),
    CONSTRAINT ck_scientific_array_nbytes_nonnegative CHECK ((array_nbytes >= 0)),
    CONSTRAINT ck_scientific_array_ordinal_nonnegative CHECK ((ordinal >= 0)),
    CONSTRAINT ck_scientific_array_payload_hash_hex CHECK (((payload_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_scientific_array_shape CHECK (((cardinality(shape) > 0) AND (array_position(shape, NULL::integer) IS NULL) AND (0 <= ALL (shape)))),
    CONSTRAINT scientific_array_kind CHECK (((kind)::text = ANY (ARRAY[('forces'::character varying)::text, ('hessian'::character varying)::text, ('vibrational_frequencies'::character varying)::text, ('reduced_masses'::character varying)::text, ('vibrational_force_constants'::character varying)::text, ('ir_intensities'::character varying)::text, ('normal_modes'::character varying)::text, ('moments_of_inertia'::character varying)::text, ('rotational_temperatures'::character varying)::text, ('rotational_constants'::character varying)::text, ('vibrational_temperatures'::character varying)::text, ('orbital_alpha_energies'::character varying)::text, ('orbital_beta_energies'::character varying)::text, ('orbital_coefficient'::character varying)::text, ('atomic_population'::character varying)::text, ('polarizability_tensor'::character varying)::text, ('electric_dipole_moment'::character varying)::text, ('dipole'::character varying)::text, ('quadrupole'::character varying)::text, ('traceless_quadrupole'::character varying)::text, ('octapole'::character varying)::text, ('hexadecapole'::character varying)::text, ('nmr_shielding_tensor'::character varying)::text, ('nmr_principal_values'::character varying)::text, ('nmr_coupling_k'::character varying)::text, ('nmr_coupling_j'::character varying)::text, ('nmr_coupling_k_component'::character varying)::text, ('nmr_coupling_j_component'::character varying)::text, ('bond_order_matrix'::character varying)::text, ('fukui_positive'::character varying)::text, ('fukui_negative'::character varying)::text, ('fukui_zero'::character varying)::text, ('fractional_occupation_density'::character varying)::text, ('transition_dipole'::character varying)::text])))
);


--
-- Name: scientific_array_assignment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scientific_array_assignment (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    scientific_array_id uuid NOT NULL,
    slot character varying(128) NOT NULL,
    slot_ordinal integer NOT NULL,
    molecular_orbital_result_id uuid,
    atomic_population_series_id uuid,
    polarizability_result_id uuid,
    nmr_result_id uuid,
    nmr_shielding_tensor_id uuid,
    bond_order_result_id uuid,
    single_point_property_result_id uuid,
    electronic_state_id uuid,
    CONSTRAINT ck_scientific_array_assignment_one_owner CHECK ((num_nonnulls(molecular_orbital_result_id, atomic_population_series_id, polarizability_result_id, nmr_result_id, nmr_shielding_tensor_id, bond_order_result_id, single_point_property_result_id, electronic_state_id) = 1)),
    CONSTRAINT ck_scientific_array_assignment_slot_ordinal_nonnegative CHECK ((slot_ordinal >= 0))
);


--
-- Name: single_point_property_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.single_point_property_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    vertical_ionization_potential_ev double precision,
    vertical_electron_affinity_ev double precision,
    global_electrophilicity_index_ev double precision,
    source_schema_version character varying(64) NOT NULL
);


--
-- Name: storage_garbage_collection_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_garbage_collection_run (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    state_id uuid NOT NULL,
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    scan_after timestamp with time zone NOT NULL,
    scan_until timestamp with time zone NOT NULL,
    status character varying(9) NOT NULL,
    objects_seen bigint DEFAULT '0'::bigint NOT NULL,
    objects_deleted bigint DEFAULT '0'::bigint NOT NULL,
    objects_retained bigint DEFAULT '0'::bigint NOT NULL,
    objects_failed bigint DEFAULT '0'::bigint NOT NULL,
    error_message text,
    CONSTRAINT ck_storage_gc_run_deleted_nonnegative CHECK ((objects_deleted >= 0)),
    CONSTRAINT ck_storage_gc_run_failed_nonnegative CHECK ((objects_failed >= 0)),
    CONSTRAINT ck_storage_gc_run_retained_nonnegative CHECK ((objects_retained >= 0)),
    CONSTRAINT ck_storage_gc_run_seen_nonnegative CHECK ((objects_seen >= 0)),
    CONSTRAINT ck_storage_gc_run_window CHECK ((scan_until >= scan_after)),
    CONSTRAINT storage_garbage_collection_run_status CHECK (((status)::text = ANY ((ARRAY['running'::character varying, 'succeeded'::character varying, 'failed'::character varying])::text[])))
);


--
-- Name: storage_garbage_collection_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_garbage_collection_state (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    bucket character varying(255) NOT NULL,
    root_prefix text NOT NULL,
    watermark_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    last_successful_run_id uuid
);


--
-- Name: thermochemistry_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thermochemistry_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    temperature_kelvin double precision NOT NULL,
    pressure_atm double precision NOT NULL,
    zpe_correction_hartree numeric(24,6),
    thermal_energy_correction_hartree numeric(24,6),
    thermal_enthalpy_correction_hartree numeric(24,6),
    thermal_gibbs_correction_hartree numeric(24,6),
    zero_point_energy_hartree numeric(24,6),
    thermal_internal_energy_hartree numeric(24,6),
    enthalpy_hartree numeric(24,6),
    gibbs_free_energy_hartree numeric(24,6),
    entropy_cal_mol_k double precision,
    heat_capacity_cv_cal_mol_k double precision,
    molecular_mass_amu double precision,
    rotational_symmetry_number integer,
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_thermochemistry_result_has_value CHECK ((num_nonnulls(zpe_correction_hartree, thermal_energy_correction_hartree, thermal_enthalpy_correction_hartree, thermal_gibbs_correction_hartree, zero_point_energy_hartree, thermal_internal_energy_hartree, enthalpy_hartree, gibbs_free_energy_hartree, entropy_cal_mol_k, heat_capacity_cv_cal_mol_k, molecular_mass_amu, rotational_symmetry_number) > 0)),
    CONSTRAINT ck_thermochemistry_result_heat_capacity_nonnegative CHECK (((heat_capacity_cv_cal_mol_k IS NULL) OR (heat_capacity_cv_cal_mol_k >= (0)::double precision))),
    CONSTRAINT ck_thermochemistry_result_mass_positive CHECK (((molecular_mass_amu IS NULL) OR (molecular_mass_amu > (0)::double precision))),
    CONSTRAINT ck_thermochemistry_result_pressure_positive CHECK ((pressure_atm > (0)::double precision)),
    CONSTRAINT ck_thermochemistry_result_symmetry_positive CHECK (((rotational_symmetry_number IS NULL) OR (rotational_symmetry_number >= 1))),
    CONSTRAINT ck_thermochemistry_result_temperature_positive CHECK ((temperature_kelvin > (0)::double precision))
);


--
-- Name: total_spin_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.total_spin_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    spin_square double precision,
    spin_quantum_number double precision,
    source_schema_version character varying(64) NOT NULL
);


--
-- Name: transition_state_endpoint; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transition_state_endpoint (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    calculation_frame_id uuid NOT NULL,
    topology_id uuid NOT NULL,
    direction character varying(8) NOT NULL,
    atom_count integer NOT NULL,
    displacement_ratio double precision NOT NULL,
    source_coordinates bytea NOT NULL,
    source_coordinate_hash character varying(64) NOT NULL,
    source_to_topology_atom_indices integer[] CONSTRAINT transition_state_endpoint_source_to_topology_atom_indi_not_null NOT NULL,
    provenance jsonb NOT NULL,
    CONSTRAINT ck_transition_state_endpoint_atom_count_positive CHECK ((atom_count > 0)),
    CONSTRAINT ck_transition_state_endpoint_coordinate_hash_hex CHECK (((source_coordinate_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_transition_state_endpoint_displacement_ratio_positive CHECK ((displacement_ratio > (0)::double precision)),
    CONSTRAINT ck_transition_state_endpoint_mapping_length CHECK ((cardinality(source_to_topology_atom_indices) = atom_count)),
    CONSTRAINT transition_state_endpoint_direction CHECK (((direction)::text = ANY ((ARRAY['negative'::character varying, 'positive'::character varying])::text[])))
);


--
-- Name: transition_state_inference; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transition_state_inference (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    artifact_ingestion_id uuid NOT NULL,
    file_frame_index integer NOT NULL,
    imaginary_mode_index integer NOT NULL,
    imaginary_frequency_cm1 double precision NOT NULL,
    status character varying(9) NOT NULL,
    inference_method character varying(128) NOT NULL,
    inference_settings jsonb NOT NULL,
    logical_reaction_id uuid,
    mapped_reaction_id uuid,
    calculation_frame_id uuid,
    error_code character varying(128),
    error_message text,
    parse_revision_id uuid NOT NULL,
    CONSTRAINT ck_transition_state_inference_failed_links CHECK ((((status)::text <> 'failed'::text) OR (num_nonnulls(logical_reaction_id, mapped_reaction_id, calculation_frame_id) = 0))),
    CONSTRAINT ck_transition_state_inference_indices_nonnegative CHECK (((file_frame_index >= 0) AND (imaginary_mode_index >= 0))),
    CONSTRAINT ck_transition_state_inference_succeeded_links CHECK ((((status)::text <> 'succeeded'::text) OR (num_nonnulls(logical_reaction_id, mapped_reaction_id, calculation_frame_id) = 3))),
    CONSTRAINT transition_state_inference_status CHECK (((status)::text = ANY ((ARRAY['succeeded'::character varying, 'failed'::character varying])::text[])))
);


--
-- Name: user_account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_account (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    display_name text NOT NULL,
    primary_email character varying(320),
    status character varying(9) DEFAULT 'active'::character varying NOT NULL,
    is_service_account boolean DEFAULT false NOT NULL,
    last_authenticated_at timestamp with time zone,
    CONSTRAINT user_account_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'suspended'::character varying])::text[])))
);


--
-- Name: vibration_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vibration_result (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    frame_id uuid NOT NULL,
    mode_count integer NOT NULL,
    imaginary_mode_count integer NOT NULL,
    lowest_frequency_cm1 double precision,
    mode_indices integer[] NOT NULL,
    axis_order character varying(32)[],
    atom_order character varying(32),
    normalization character varying(64),
    mass_weighting character varying(64),
    source_schema_version character varying(64) NOT NULL,
    CONSTRAINT ck_vibration_result_mode_counts CHECK (((mode_count >= 0) AND (imaginary_mode_count >= 0) AND (imaginary_mode_count <= mode_count))),
    CONSTRAINT ck_vibration_result_mode_indices_count CHECK ((cardinality(mode_indices) = mode_count))
);


--
-- Name: workflow_manifest; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_manifest (
    id uuid DEFAULT uuidv7() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    artifact_file_id uuid NOT NULL,
    manifest_key text NOT NULL,
    revision integer NOT NULL,
    schema_version character varying(64) NOT NULL,
    payload_sha256 character varying(64) NOT NULL,
    qc_policy_version character varying(64) NOT NULL,
    status character varying(10) DEFAULT 'received'::character varying NOT NULL,
    supersedes_id uuid,
    validation_metadata jsonb NOT NULL,
    published_at timestamp with time zone,
    CONSTRAINT ck_workflow_manifest_not_self_superseding CHECK (((supersedes_id IS NULL) OR (supersedes_id <> id))),
    CONSTRAINT ck_workflow_manifest_payload_hash_hex CHECK (((payload_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_workflow_manifest_published_timestamp CHECK ((((status)::text <> 'published'::text) OR (published_at IS NOT NULL))),
    CONSTRAINT ck_workflow_manifest_revision_positive CHECK ((revision >= 1)),
    CONSTRAINT ck_workflow_manifest_unpublished_timestamp CHECK ((((status)::text <> ALL ((ARRAY['received'::character varying, 'validated'::character varying, 'rejected'::character varying])::text[])) OR (published_at IS NULL))),
    CONSTRAINT workflow_manifest_status CHECK (((status)::text = ANY ((ARRAY['received'::character varying, 'validated'::character varying, 'published'::character varying, 'rejected'::character varying, 'superseded'::character varying])::text[])))
);


--
-- Name: artifact_file artifact_file_content_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_file
    ADD CONSTRAINT artifact_file_content_sha256_key UNIQUE (content_sha256);


--
-- Name: artifact_file artifact_file_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_file
    ADD CONSTRAINT artifact_file_pkey PRIMARY KEY (id);


--
-- Name: atomic_population_series atomic_population_series_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.atomic_population_series
    ADD CONSTRAINT atomic_population_series_pkey PRIMARY KEY (id);


--
-- Name: bond_order_result bond_order_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bond_order_result
    ADD CONSTRAINT bond_order_result_frame_id_key UNIQUE (frame_id);


--
-- Name: bond_order_result bond_order_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bond_order_result
    ADD CONSTRAINT bond_order_result_pkey PRIMARY KEY (id);


--
-- Name: calculation_frame calculation_frame_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_frame
    ADD CONSTRAINT calculation_frame_pkey PRIMARY KEY (id);


--
-- Name: calculation_protocol calculation_protocol_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_protocol
    ADD CONSTRAINT calculation_protocol_pkey PRIMARY KEY (id);


--
-- Name: calculation_protocol calculation_protocol_protocol_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_protocol
    ADD CONSTRAINT calculation_protocol_protocol_hash_key UNIQUE (protocol_hash);


--
-- Name: calculation_segment calculation_segment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_segment
    ADD CONSTRAINT calculation_segment_pkey PRIMARY KEY (id);


--
-- Name: calculation_status_result calculation_status_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_status_result
    ADD CONSTRAINT calculation_status_result_pkey PRIMARY KEY (id);


--
-- Name: charge_spin_population_result charge_spin_population_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.charge_spin_population_result
    ADD CONSTRAINT charge_spin_population_result_frame_id_key UNIQUE (frame_id);


--
-- Name: charge_spin_population_result charge_spin_population_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.charge_spin_population_result
    ADD CONSTRAINT charge_spin_population_result_pkey PRIMARY KEY (id);


--
-- Name: electronic_configuration electronic_configuration_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_configuration
    ADD CONSTRAINT electronic_configuration_pkey PRIMARY KEY (id);


--
-- Name: electronic_state electronic_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_state
    ADD CONSTRAINT electronic_state_pkey PRIMARY KEY (id);


--
-- Name: electronic_state_set electronic_state_set_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_state_set
    ADD CONSTRAINT electronic_state_set_pkey PRIMARY KEY (id);


--
-- Name: energy_observation energy_observation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.energy_observation
    ADD CONSTRAINT energy_observation_pkey PRIMARY KEY (id);


--
-- Name: frame_energy_result frame_energy_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.frame_energy_result
    ADD CONSTRAINT frame_energy_result_pkey PRIMARY KEY (id);


--
-- Name: geometry_optimization_result geometry_optimization_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geometry_optimization_result
    ADD CONSTRAINT geometry_optimization_result_pkey PRIMARY KEY (id);


--
-- Name: geometry geometry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geometry
    ADD CONSTRAINT geometry_pkey PRIMARY KEY (id);


--
-- Name: implicit_solvation_result implicit_solvation_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.implicit_solvation_result
    ADD CONSTRAINT implicit_solvation_result_frame_id_key UNIQUE (frame_id);


--
-- Name: implicit_solvation_result implicit_solvation_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.implicit_solvation_result
    ADD CONSTRAINT implicit_solvation_result_pkey PRIMARY KEY (id);


--
-- Name: logical_reaction_participant logical_reaction_participant_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.logical_reaction_participant
    ADD CONSTRAINT logical_reaction_participant_pkey PRIMARY KEY (id);


--
-- Name: logical_reaction logical_reaction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.logical_reaction
    ADD CONSTRAINT logical_reaction_pkey PRIMARY KEY (id);


--
-- Name: manifest_artifact_binding manifest_artifact_binding_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_artifact_binding
    ADD CONSTRAINT manifest_artifact_binding_pkey PRIMARY KEY (id);


--
-- Name: mapped_reaction_edge mapped_reaction_edge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_edge
    ADD CONSTRAINT mapped_reaction_edge_pkey PRIMARY KEY (id);


--
-- Name: mapped_reaction_node_geometry_mapping mapped_reaction_node_geometry_mapping_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry_mapping
    ADD CONSTRAINT mapped_reaction_node_geometry_mapping_pkey PRIMARY KEY (id);


--
-- Name: mapped_reaction_node_geometry mapped_reaction_node_geometry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry
    ADD CONSTRAINT mapped_reaction_node_geometry_pkey PRIMARY KEY (id);


--
-- Name: mapped_reaction_node mapped_reaction_node_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node
    ADD CONSTRAINT mapped_reaction_node_pkey PRIMARY KEY (id);


--
-- Name: mapped_reaction_participant mapped_reaction_participant_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_participant
    ADD CONSTRAINT mapped_reaction_participant_pkey PRIMARY KEY (id);


--
-- Name: mapped_reaction mapped_reaction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction
    ADD CONSTRAINT mapped_reaction_pkey PRIMARY KEY (id);


--
-- Name: mapped_reaction_thermodynamic_profile mapped_reaction_thermodynamic_profile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_thermodynamic_profile
    ADD CONSTRAINT mapped_reaction_thermodynamic_profile_pkey PRIMARY KEY (id);


--
-- Name: molecular_formula molecular_formula_composition_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_formula
    ADD CONSTRAINT molecular_formula_composition_hash_key UNIQUE (composition_hash);


--
-- Name: molecular_formula molecular_formula_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_formula
    ADD CONSTRAINT molecular_formula_pkey PRIMARY KEY (id);


--
-- Name: molecular_orbital_result molecular_orbital_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_orbital_result
    ADD CONSTRAINT molecular_orbital_result_frame_id_key UNIQUE (frame_id);


--
-- Name: molecular_orbital_result molecular_orbital_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_orbital_result
    ADD CONSTRAINT molecular_orbital_result_pkey PRIMARY KEY (id);


--
-- Name: molecular_topology molecular_topology_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_topology
    ADD CONSTRAINT molecular_topology_pkey PRIMARY KEY (id);


--
-- Name: multireference_result multireference_result_electronic_state_set_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.multireference_result
    ADD CONSTRAINT multireference_result_electronic_state_set_id_key UNIQUE (electronic_state_set_id);


--
-- Name: multireference_result multireference_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.multireference_result
    ADD CONSTRAINT multireference_result_frame_id_key UNIQUE (frame_id);


--
-- Name: multireference_result multireference_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.multireference_result
    ADD CONSTRAINT multireference_result_pkey PRIMARY KEY (id);


--
-- Name: nmr_result nmr_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nmr_result
    ADD CONSTRAINT nmr_result_frame_id_key UNIQUE (frame_id);


--
-- Name: nmr_result nmr_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nmr_result
    ADD CONSTRAINT nmr_result_pkey PRIMARY KEY (id);


--
-- Name: nmr_shielding_tensor nmr_shielding_tensor_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nmr_shielding_tensor
    ADD CONSTRAINT nmr_shielding_tensor_pkey PRIMARY KEY (id);


--
-- Name: parse_revision parse_revision_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parse_revision
    ADD CONSTRAINT parse_revision_pkey PRIMARY KEY (id);


--
-- Name: artifact_ingestion pk_artifact_ingestion; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_ingestion
    ADD CONSTRAINT pk_artifact_ingestion PRIMARY KEY (id);


--
-- Name: audit_event pk_audit_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_event
    ADD CONSTRAINT pk_audit_event PRIMARY KEY (id);


--
-- Name: auth_session pk_auth_session; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_session
    ADD CONSTRAINT pk_auth_session PRIMARY KEY (id);


--
-- Name: external_identity pk_external_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_identity
    ADD CONSTRAINT pk_external_identity PRIMARY KEY (id);


--
-- Name: mcp_access_token pk_mcp_access_token; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_access_token
    ADD CONSTRAINT pk_mcp_access_token PRIMARY KEY (id);


--
-- Name: molecular_topology_derivation pk_molecular_topology_derivation; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_topology_derivation
    ADD CONSTRAINT pk_molecular_topology_derivation PRIMARY KEY (id);


--
-- Name: organization pk_organization; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization
    ADD CONSTRAINT pk_organization PRIMARY KEY (id);


--
-- Name: organization_membership pk_organization_membership; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_membership
    ADD CONSTRAINT pk_organization_membership PRIMARY KEY (id);


--
-- Name: project pk_project; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project
    ADD CONSTRAINT pk_project PRIMARY KEY (id);


--
-- Name: project_invitation pk_project_invitation; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_invitation
    ADD CONSTRAINT pk_project_invitation PRIMARY KEY (id);


--
-- Name: project_membership pk_project_membership; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_membership
    ADD CONSTRAINT pk_project_membership PRIMARY KEY (id);


--
-- Name: storage_garbage_collection_run pk_storage_garbage_collection_run; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_garbage_collection_run
    ADD CONSTRAINT pk_storage_garbage_collection_run PRIMARY KEY (id);


--
-- Name: storage_garbage_collection_state pk_storage_garbage_collection_state; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_garbage_collection_state
    ADD CONSTRAINT pk_storage_garbage_collection_state PRIMARY KEY (id);


--
-- Name: transition_state_endpoint pk_transition_state_endpoint; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_endpoint
    ADD CONSTRAINT pk_transition_state_endpoint PRIMARY KEY (id);


--
-- Name: transition_state_inference pk_transition_state_inference; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_inference
    ADD CONSTRAINT pk_transition_state_inference PRIMARY KEY (id);


--
-- Name: user_account pk_user_account; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_account
    ADD CONSTRAINT pk_user_account PRIMARY KEY (id);


--
-- Name: polarizability_result polarizability_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.polarizability_result
    ADD CONSTRAINT polarizability_result_frame_id_key UNIQUE (frame_id);


--
-- Name: polarizability_result polarizability_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.polarizability_result
    ADD CONSTRAINT polarizability_result_pkey PRIMARY KEY (id);


--
-- Name: scientific_array_assignment scientific_array_assignment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_pkey PRIMARY KEY (id);


--
-- Name: scientific_array_assignment scientific_array_assignment_scientific_array_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_scientific_array_id_key UNIQUE (scientific_array_id);


--
-- Name: scientific_array scientific_array_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array
    ADD CONSTRAINT scientific_array_pkey PRIMARY KEY (id);


--
-- Name: single_point_property_result single_point_property_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.single_point_property_result
    ADD CONSTRAINT single_point_property_result_frame_id_key UNIQUE (frame_id);


--
-- Name: single_point_property_result single_point_property_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.single_point_property_result
    ADD CONSTRAINT single_point_property_result_pkey PRIMARY KEY (id);


--
-- Name: thermochemistry_result thermochemistry_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thermochemistry_result
    ADD CONSTRAINT thermochemistry_result_frame_id_key UNIQUE (frame_id);


--
-- Name: thermochemistry_result thermochemistry_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thermochemistry_result
    ADD CONSTRAINT thermochemistry_result_pkey PRIMARY KEY (id);


--
-- Name: total_spin_result total_spin_result_frame_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.total_spin_result
    ADD CONSTRAINT total_spin_result_frame_id_key UNIQUE (frame_id);


--
-- Name: total_spin_result total_spin_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.total_spin_result
    ADD CONSTRAINT total_spin_result_pkey PRIMARY KEY (id);


--
-- Name: artifact_file uq_artifact_file_object; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_file
    ADD CONSTRAINT uq_artifact_file_object UNIQUE (bucket, object_key);


--
-- Name: artifact_ingestion uq_artifact_ingestion_artifact_file_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_ingestion
    ADD CONSTRAINT uq_artifact_ingestion_artifact_file_id UNIQUE (artifact_file_id);


--
-- Name: atomic_population_series uq_atomic_population_series_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.atomic_population_series
    ADD CONSTRAINT uq_atomic_population_series_key UNIQUE (result_id, series_key);


--
-- Name: calculation_frame uq_calculation_frame_id_geometry; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_frame
    ADD CONSTRAINT uq_calculation_frame_id_geometry UNIQUE (id, geometry_id);


--
-- Name: calculation_frame uq_calculation_frame_revision_file_index; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_frame
    ADD CONSTRAINT uq_calculation_frame_revision_file_index UNIQUE (parse_revision_id, file_frame_index);


--
-- Name: calculation_frame uq_calculation_frame_segment_index; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_frame
    ADD CONSTRAINT uq_calculation_frame_segment_index UNIQUE (segment_id, frame_index);


--
-- Name: calculation_segment uq_calculation_segment_id_revision; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_segment
    ADD CONSTRAINT uq_calculation_segment_id_revision UNIQUE (id, parse_revision_id);


--
-- Name: calculation_segment uq_calculation_segment_revision_index; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_segment
    ADD CONSTRAINT uq_calculation_segment_revision_index UNIQUE (parse_revision_id, segment_index);


--
-- Name: calculation_status_result uq_calculation_status_result_frame_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_status_result
    ADD CONSTRAINT uq_calculation_status_result_frame_id UNIQUE (frame_id);


--
-- Name: electronic_configuration uq_electronic_configuration_ordinal; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_configuration
    ADD CONSTRAINT uq_electronic_configuration_ordinal UNIQUE (electronic_state_id, configuration_ordinal);


--
-- Name: electronic_state uq_electronic_state_ordinal; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_state
    ADD CONSTRAINT uq_electronic_state_ordinal UNIQUE (state_set_id, state_ordinal);


--
-- Name: electronic_state_set uq_electronic_state_set_frame_kind; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_state_set
    ADD CONSTRAINT uq_electronic_state_set_frame_kind UNIQUE (frame_id, kind);


--
-- Name: energy_observation uq_energy_observation_result_index; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.energy_observation
    ADD CONSTRAINT uq_energy_observation_result_index UNIQUE (energy_result_id, observation_index);


--
-- Name: external_identity uq_external_identity_issuer_subject; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_identity
    ADD CONSTRAINT uq_external_identity_issuer_subject UNIQUE (issuer, subject);


--
-- Name: frame_energy_result uq_frame_energy_result_frame_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.frame_energy_result
    ADD CONSTRAINT uq_frame_energy_result_frame_id UNIQUE (frame_id);


--
-- Name: geometry_optimization_result uq_geometry_optimization_result_frame_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geometry_optimization_result
    ADD CONSTRAINT uq_geometry_optimization_result_frame_id UNIQUE (frame_id);


--
-- Name: geometry uq_geometry_topology_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geometry
    ADD CONSTRAINT uq_geometry_topology_hash UNIQUE (topology_id, canonicalization_version, geometry_hash);


--
-- Name: logical_reaction uq_logical_reaction_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.logical_reaction
    ADD CONSTRAINT uq_logical_reaction_hash UNIQUE (reaction_hash);


--
-- Name: logical_reaction_participant uq_logical_reaction_participant_side_index; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.logical_reaction_participant
    ADD CONSTRAINT uq_logical_reaction_participant_side_index UNIQUE (logical_reaction_id, side, participant_index);


--
-- Name: manifest_artifact_binding uq_manifest_artifact_binding_manifest_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_artifact_binding
    ADD CONSTRAINT uq_manifest_artifact_binding_manifest_key UNIQUE (workflow_manifest_id, artifact_key);


--
-- Name: mapped_reaction_node_geometry uq_mapped_node_geometry_component_coordinate; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry
    ADD CONSTRAINT uq_mapped_node_geometry_component_coordinate UNIQUE (mapped_reaction_node_id, component_key, coordinate_index);


--
-- Name: mapped_reaction_node_geometry uq_mapped_node_geometry_id_geometry; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry
    ADD CONSTRAINT uq_mapped_node_geometry_id_geometry UNIQUE (id, geometry_id);


--
-- Name: mapped_reaction_node_geometry uq_mapped_node_geometry_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry
    ADD CONSTRAINT uq_mapped_node_geometry_identity UNIQUE NULLS NOT DISTINCT (mapped_reaction_node_id, geometry_id, mapped_reaction_participant_id);


--
-- Name: mapped_reaction_participant uq_mapped_participant_logical; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_participant
    ADD CONSTRAINT uq_mapped_participant_logical UNIQUE (mapped_reaction_id, logical_reaction_participant_id);


--
-- Name: mapped_reaction_participant uq_mapped_participant_template; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_participant
    ADD CONSTRAINT uq_mapped_participant_template UNIQUE (mapped_reaction_id, side, template_index);


--
-- Name: mapped_reaction_edge uq_mapped_reaction_edge_path_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_edge
    ADD CONSTRAINT uq_mapped_reaction_edge_path_id UNIQUE (mapped_reaction_id, id);


--
-- Name: mapped_reaction_edge uq_mapped_reaction_edge_path_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_edge
    ADD CONSTRAINT uq_mapped_reaction_edge_path_key UNIQUE (mapped_reaction_id, edge_key);


--
-- Name: mapped_reaction uq_mapped_reaction_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction
    ADD CONSTRAINT uq_mapped_reaction_hash UNIQUE (logical_reaction_id, mapping_hash);


--
-- Name: mapped_reaction uq_mapped_reaction_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction
    ADD CONSTRAINT uq_mapped_reaction_key UNIQUE (logical_reaction_id, mapped_reaction_key);


--
-- Name: mapped_reaction_node_geometry_mapping uq_mapped_reaction_node_geometry_mapping_geometry; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry_mapping
    ADD CONSTRAINT uq_mapped_reaction_node_geometry_mapping_geometry UNIQUE (mapped_reaction_node_geometry_id);


--
-- Name: mapped_reaction_node uq_mapped_reaction_node_parent_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node
    ADD CONSTRAINT uq_mapped_reaction_node_parent_id UNIQUE (mapped_reaction_id, id);


--
-- Name: mapped_reaction_node uq_mapped_reaction_node_parent_index; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node
    ADD CONSTRAINT uq_mapped_reaction_node_parent_index UNIQUE (mapped_reaction_id, node_index);


--
-- Name: mapped_reaction_node uq_mapped_reaction_node_parent_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node
    ADD CONSTRAINT uq_mapped_reaction_node_parent_key UNIQUE (mapped_reaction_id, node_key);


--
-- Name: mapped_reaction_thermodynamic_profile uq_mapped_reaction_thermodynamic_source; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_thermodynamic_profile
    ADD CONSTRAINT uq_mapped_reaction_thermodynamic_source UNIQUE (mapped_reaction_id, source_key_hash);


--
-- Name: molecular_topology_derivation uq_molecular_topology_derivation_id_topology; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_topology_derivation
    ADD CONSTRAINT uq_molecular_topology_derivation_id_topology UNIQUE (id, topology_id);


--
-- Name: molecular_topology_derivation uq_molecular_topology_derivation_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_topology_derivation
    ADD CONSTRAINT uq_molecular_topology_derivation_identity UNIQUE (topology_id, provenance_schema_version, provenance_hash);


--
-- Name: molecular_topology uq_molecular_topology_identity_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_topology
    ADD CONSTRAINT uq_molecular_topology_identity_hash UNIQUE (identity_schema_version, graph_hash);


--
-- Name: nmr_shielding_tensor uq_nmr_shielding_tensor_atom; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nmr_shielding_tensor
    ADD CONSTRAINT uq_nmr_shielding_tensor_atom UNIQUE (result_id, atom_index);


--
-- Name: organization_membership uq_organization_membership_organization_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_membership
    ADD CONSTRAINT uq_organization_membership_organization_user UNIQUE (organization_id, user_id);


--
-- Name: organization uq_organization_slug; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization
    ADD CONSTRAINT uq_organization_slug UNIQUE (slug);


--
-- Name: parse_revision uq_parse_revision_artifact_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parse_revision
    ADD CONSTRAINT uq_parse_revision_artifact_number UNIQUE (artifact_file_id, revision_number);


--
-- Name: project_membership uq_project_membership_project_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_membership
    ADD CONSTRAINT uq_project_membership_project_user UNIQUE (project_id, user_id);


--
-- Name: project uq_project_organization_slug; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project
    ADD CONSTRAINT uq_project_organization_slug UNIQUE (organization_id, slug);


--
-- Name: scientific_array_assignment uq_scientific_array_assignment_bond_order; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT uq_scientific_array_assignment_bond_order UNIQUE (bond_order_result_id, slot, slot_ordinal);


--
-- Name: scientific_array_assignment uq_scientific_array_assignment_electronic_state; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT uq_scientific_array_assignment_electronic_state UNIQUE (electronic_state_id, slot, slot_ordinal);


--
-- Name: scientific_array_assignment uq_scientific_array_assignment_molecular_orbital; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT uq_scientific_array_assignment_molecular_orbital UNIQUE (molecular_orbital_result_id, slot, slot_ordinal);


--
-- Name: scientific_array_assignment uq_scientific_array_assignment_nmr; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT uq_scientific_array_assignment_nmr UNIQUE (nmr_result_id, slot, slot_ordinal);


--
-- Name: scientific_array_assignment uq_scientific_array_assignment_nmr_shielding; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT uq_scientific_array_assignment_nmr_shielding UNIQUE (nmr_shielding_tensor_id, slot, slot_ordinal);


--
-- Name: scientific_array_assignment uq_scientific_array_assignment_polarizability; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT uq_scientific_array_assignment_polarizability UNIQUE (polarizability_result_id, slot, slot_ordinal);


--
-- Name: scientific_array_assignment uq_scientific_array_assignment_population; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT uq_scientific_array_assignment_population UNIQUE (atomic_population_series_id, slot, slot_ordinal);


--
-- Name: scientific_array_assignment uq_scientific_array_assignment_single_point; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT uq_scientific_array_assignment_single_point UNIQUE (single_point_property_result_id, slot, slot_ordinal);


--
-- Name: scientific_array uq_scientific_array_frame_kind_ordinal; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array
    ADD CONSTRAINT uq_scientific_array_frame_kind_ordinal UNIQUE (frame_id, kind, ordinal);


--
-- Name: storage_garbage_collection_state uq_storage_gc_state_bucket_prefix; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_garbage_collection_state
    ADD CONSTRAINT uq_storage_gc_state_bucket_prefix UNIQUE (bucket, root_prefix);


--
-- Name: transition_state_endpoint uq_transition_state_endpoint_frame_direction; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_endpoint
    ADD CONSTRAINT uq_transition_state_endpoint_frame_direction UNIQUE (calculation_frame_id, direction);


--
-- Name: transition_state_inference uq_transition_state_inference_revision_frame; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_inference
    ADD CONSTRAINT uq_transition_state_inference_revision_frame UNIQUE (parse_revision_id, file_frame_index);


--
-- Name: vibration_result uq_vibration_result_frame_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vibration_result
    ADD CONSTRAINT uq_vibration_result_frame_id UNIQUE (frame_id);


--
-- Name: workflow_manifest uq_workflow_manifest_key_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_manifest
    ADD CONSTRAINT uq_workflow_manifest_key_id UNIQUE (manifest_key, id);


--
-- Name: workflow_manifest uq_workflow_manifest_key_revision; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_manifest
    ADD CONSTRAINT uq_workflow_manifest_key_revision UNIQUE (manifest_key, revision);


--
-- Name: vibration_result vibration_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vibration_result
    ADD CONSTRAINT vibration_result_pkey PRIMARY KEY (id);


--
-- Name: workflow_manifest workflow_manifest_artifact_file_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_manifest
    ADD CONSTRAINT workflow_manifest_artifact_file_id_key UNIQUE (artifact_file_id);


--
-- Name: workflow_manifest workflow_manifest_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_manifest
    ADD CONSTRAINT workflow_manifest_pkey PRIMARY KEY (id);


--
-- Name: ix_artifact_file_artifact_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_artifact_kind ON public.artifact_file USING btree (artifact_kind);


--
-- Name: ix_artifact_file_created_by_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_created_by_user_id ON public.artifact_file USING btree (created_by_user_id);


--
-- Name: ix_artifact_file_original_filename_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_original_filename_trgm ON public.artifact_file USING gin (original_filename public.gin_trgm_ops);


--
-- Name: ix_artifact_file_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_project_id ON public.artifact_file USING btree (project_id);


--
-- Name: ix_artifact_file_project_status_created_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_project_status_created_id ON public.artifact_file USING btree (project_id, storage_status, created_at, id);


--
-- Name: ix_artifact_file_storage_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_storage_status ON public.artifact_file USING btree (storage_status);


--
-- Name: ix_artifact_file_storage_status_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_storage_status_created_at ON public.artifact_file USING btree (storage_status, created_at);


--
-- Name: ix_artifact_file_visibility; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_visibility ON public.artifact_file USING btree (visibility);


--
-- Name: ix_artifact_file_visibility_status_created_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_file_visibility_status_created_id ON public.artifact_file USING btree (visibility, storage_status, created_at, id);


--
-- Name: ix_artifact_ingestion_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifact_ingestion_status ON public.artifact_ingestion USING btree (status);


--
-- Name: ix_atomic_population_series_result_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_atomic_population_series_result_id ON public.atomic_population_series USING btree (result_id);


--
-- Name: ix_audit_event_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_event_action ON public.audit_event USING btree (action);


--
-- Name: ix_audit_event_actor_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_event_actor_user_id ON public.audit_event USING btree (actor_user_id);


--
-- Name: ix_audit_event_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_event_entity_id ON public.audit_event USING btree (entity_id);


--
-- Name: ix_audit_event_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_event_project_id ON public.audit_event USING btree (project_id);


--
-- Name: ix_auth_session_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_session_expires_at ON public.auth_session USING btree (expires_at);


--
-- Name: ix_auth_session_revoked_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_session_revoked_at ON public.auth_session USING btree (revoked_at);


--
-- Name: ix_auth_session_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_auth_session_token_hash ON public.auth_session USING btree (token_hash);


--
-- Name: ix_auth_session_user_active_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_session_user_active_last_seen ON public.auth_session USING btree (user_id, last_seen_at) WHERE (revoked_at IS NULL);


--
-- Name: ix_auth_session_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_session_user_id ON public.auth_session USING btree (user_id);


--
-- Name: ix_calculation_frame_converged_geometry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_frame_converged_geometry ON public.calculation_frame USING btree (geometry_id) WHERE ((optimization_status)::text = 'converged'::text);


--
-- Name: ix_calculation_frame_frame_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_frame_frame_role ON public.calculation_frame USING btree (frame_role);


--
-- Name: ix_calculation_frame_frequency_counts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_frame_frequency_counts ON public.calculation_frame USING btree (frequency_count, negative_frequency_count);


--
-- Name: ix_calculation_frame_geometry_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_frame_geometry_id ON public.calculation_frame USING btree (geometry_id);

--
-- Name: ix_calculation_frame_parse_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_frame_parse_revision_id ON public.calculation_frame USING btree (parse_revision_id);


--
-- Name: ix_calculation_frame_segment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_frame_segment_id ON public.calculation_frame USING btree (segment_id);


--
-- Name: ix_calculation_frame_topology_derivation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_frame_topology_derivation_id ON public.calculation_frame USING btree (topology_derivation_id);


--
-- Name: ix_calculation_protocol_basis_set; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_protocol_basis_set ON public.calculation_protocol USING btree (basis_set);


--
-- Name: ix_calculation_protocol_method; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_protocol_method ON public.calculation_protocol USING btree (method);


--
-- Name: ix_calculation_protocol_solvent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_protocol_solvent ON public.calculation_protocol USING btree (solvent);


--
-- Name: ix_calculation_segment_parse_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_segment_parse_revision_id ON public.calculation_segment USING btree (parse_revision_id);


--
-- Name: ix_calculation_segment_protocol_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calculation_segment_protocol_id ON public.calculation_segment USING btree (protocol_id);


--
-- Name: ix_electronic_configuration_electronic_state_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_electronic_configuration_electronic_state_id ON public.electronic_configuration USING btree (electronic_state_id);


--
-- Name: ix_electronic_state_set_frame_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_electronic_state_set_frame_id ON public.electronic_state_set USING btree (frame_id);


--
-- Name: ix_electronic_state_state_set_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_electronic_state_state_set_id ON public.electronic_state USING btree (state_set_id);


--
-- Name: ix_energy_observation_energy_result_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_energy_observation_energy_result_id ON public.energy_observation USING btree (energy_result_id);


--
-- Name: ix_energy_observation_method; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_energy_observation_method ON public.energy_observation USING btree (method);


--
-- Name: ix_energy_observation_quantity_semantics; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_energy_observation_quantity_semantics ON public.energy_observation USING btree (quantity_semantics);


--
-- Name: ix_external_identity_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_external_identity_user_id ON public.external_identity USING btree (user_id);


--
-- Name: ix_geometry_topology_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_geometry_topology_id ON public.geometry USING btree (topology_id);

--
-- Name: ix_logical_reaction_cycloaddition_pattern; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_logical_reaction_cycloaddition_pattern ON public.logical_reaction USING btree (cycloaddition_pattern);


--
-- Name: ix_logical_reaction_participant_logical_reaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_logical_reaction_participant_logical_reaction_id ON public.logical_reaction_participant USING btree (logical_reaction_id);


--
-- Name: ix_logical_reaction_participant_side; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_logical_reaction_participant_side ON public.logical_reaction_participant USING btree (side);


--
-- Name: ix_logical_reaction_participant_topology_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_logical_reaction_participant_topology_id ON public.logical_reaction_participant USING btree (topology_id);


--
-- Name: ix_logical_reaction_reaction_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_logical_reaction_reaction_class ON public.logical_reaction USING btree (reaction_class);


--
-- Name: ix_logical_reaction_reaction_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_logical_reaction_reaction_hash ON public.logical_reaction USING btree (reaction_hash);

--
-- Name: ix_manifest_artifact_binding_artifact_file_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_manifest_artifact_binding_artifact_file_id ON public.manifest_artifact_binding USING btree (artifact_file_id);


--
-- Name: ix_manifest_artifact_binding_artifact_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_manifest_artifact_binding_artifact_role ON public.manifest_artifact_binding USING btree (artifact_role);


--
-- Name: ix_manifest_artifact_binding_resolution_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_manifest_artifact_binding_resolution_status ON public.manifest_artifact_binding USING btree (resolution_status);


--
-- Name: ix_manifest_artifact_binding_workflow_manifest_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_manifest_artifact_binding_workflow_manifest_id ON public.manifest_artifact_binding USING btree (workflow_manifest_id);


--
-- Name: ix_mapped_reaction_edge_edge_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_edge_edge_kind ON public.mapped_reaction_edge USING btree (edge_kind);


--
-- Name: ix_mapped_reaction_edge_mapped_reaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_edge_mapped_reaction_id ON public.mapped_reaction_edge USING btree (mapped_reaction_id);


--
-- Name: ix_mapped_reaction_edge_source_node_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_edge_source_node_id ON public.mapped_reaction_edge USING btree (source_node_id);


--
-- Name: ix_mapped_reaction_edge_target_node_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_edge_target_node_id ON public.mapped_reaction_edge USING btree (target_node_id);


--
-- Name: ix_mapped_reaction_edge_transition_state_node_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_edge_transition_state_node_id ON public.mapped_reaction_edge USING btree (transition_state_node_id);


--
-- Name: ix_mapped_reaction_logical_reaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_logical_reaction_id ON public.mapped_reaction USING btree (logical_reaction_id);


--
-- Name: ix_mapped_reaction_mapped_reaction_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_mapped_reaction_kind ON public.mapped_reaction USING btree (mapped_reaction_kind);


--
-- Name: ix_mapped_reaction_mapping_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_mapping_hash ON public.mapped_reaction USING btree (mapping_hash);


--
-- Name: ix_mapped_reaction_max_activation_gibbs; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_max_activation_gibbs ON public.mapped_reaction USING btree (maximum_activation_gibbs_free_energy_kcal_mol);


--
-- Name: ix_mapped_reaction_max_reaction_gibbs; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_max_reaction_gibbs ON public.mapped_reaction USING btree (maximum_reaction_gibbs_free_energy_kcal_mol);


--
-- Name: ix_mapped_reaction_min_activation_gibbs; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_min_activation_gibbs ON public.mapped_reaction USING btree (minimum_activation_gibbs_free_energy_kcal_mol);


--
-- Name: ix_mapped_reaction_min_reaction_gibbs; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_min_reaction_gibbs ON public.mapped_reaction USING btree (minimum_reaction_gibbs_free_energy_kcal_mol);


--
-- Name: ix_mapped_reaction_node_geometry_geometry_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_node_geometry_geometry_id ON public.mapped_reaction_node_geometry USING btree (geometry_id);


--
-- Name: ix_mapped_reaction_node_geometry_mapped_reaction_node_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_node_geometry_mapped_reaction_node_id ON public.mapped_reaction_node_geometry USING btree (mapped_reaction_node_id);


--
-- Name: ix_mapped_reaction_node_geometry_mapped_reaction_participant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_node_geometry_mapped_reaction_participant_id ON public.mapped_reaction_node_geometry USING btree (mapped_reaction_participant_id);


--
-- Name: ix_mapped_reaction_node_geometry_mapping_mapped_reactio_178a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_node_geometry_mapping_mapped_reactio_178a ON public.mapped_reaction_node_geometry_mapping USING btree (mapped_reaction_node_geometry_id);


--
-- Name: ix_mapped_reaction_node_mapped_reaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_node_mapped_reaction_id ON public.mapped_reaction_node USING btree (mapped_reaction_id);


--
-- Name: ix_mapped_reaction_node_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_node_role ON public.mapped_reaction_node USING btree (role);


--
-- Name: ix_mapped_reaction_participant_logical_reaction_participant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_participant_logical_reaction_participant_id ON public.mapped_reaction_participant USING btree (logical_reaction_participant_id);


--
-- Name: ix_mapped_reaction_participant_mapped_reaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_participant_mapped_reaction_id ON public.mapped_reaction_participant USING btree (mapped_reaction_id);


--
-- Name: ix_mapped_reaction_reaction_gist; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_reaction_gist ON public.mapped_reaction USING gist (reaction);


--
-- Name: ix_mapped_reaction_structural_bfp_gist; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_structural_bfp_gist ON public.mapped_reaction USING gist (reaction_structural_bfp);


--
-- Name: ix_mapped_reaction_thermodynamic_activation_gibbs; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_thermodynamic_activation_gibbs ON public.mapped_reaction_thermodynamic_profile USING btree (mapped_reaction_id, activation_gibbs_free_energy_kcal_mol);


--
-- Name: ix_mapped_reaction_thermodynamic_profile_mapped_reaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mapped_reaction_thermodynamic_profile_mapped_reaction_id ON public.mapped_reaction_thermodynamic_profile USING btree (mapped_reaction_id);


--
-- Name: ix_mcp_access_token_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_access_token_expires_at ON public.mcp_access_token USING btree (expires_at);


--
-- Name: ix_mcp_access_token_revoked_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_access_token_revoked_at ON public.mcp_access_token USING btree (revoked_at);


--
-- Name: ix_mcp_access_token_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_mcp_access_token_token_hash ON public.mcp_access_token USING btree (token_hash);


--
-- Name: ix_mcp_access_token_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_access_token_user_active ON public.mcp_access_token USING btree (user_id, revoked_at, expires_at);


--
-- Name: ix_mcp_access_token_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_access_token_user_id ON public.mcp_access_token USING btree (user_id);


--
-- Name: ix_molecular_formula_element_count_tokens_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_molecular_formula_element_count_tokens_gin ON public.molecular_formula USING gin (element_count_tokens);


--
-- Name: ix_molecular_formula_hill_formula; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_molecular_formula_hill_formula ON public.molecular_formula USING btree (hill_formula);


--
-- Name: ix_molecular_topology_canonical_isomeric_smiles; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_molecular_topology_canonical_isomeric_smiles ON public.molecular_topology USING btree (canonical_isomeric_smiles);


--
-- Name: ix_molecular_topology_derivation_topology_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_molecular_topology_derivation_topology_id ON public.molecular_topology_derivation USING btree (topology_id);


--
-- Name: ix_molecular_topology_formal_charge; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_molecular_topology_formal_charge ON public.molecular_topology USING btree (formal_charge);


--
-- Name: ix_molecular_topology_formula_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_molecular_topology_formula_id ON public.molecular_topology USING btree (formula_id);


--
-- Name: ix_molecular_topology_mol_gist; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_molecular_topology_mol_gist ON public.molecular_topology USING gist (mol);


--
-- Name: ix_molecular_topology_morgan_bfp_gist; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_molecular_topology_morgan_bfp_gist ON public.molecular_topology USING gist (morgan_bfp);


--
-- Name: ix_nmr_shielding_tensor_result_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_nmr_shielding_tensor_result_id ON public.nmr_shielding_tensor USING btree (result_id);


--
-- Name: ix_organization_membership_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organization_membership_organization_id ON public.organization_membership USING btree (organization_id);


--
-- Name: ix_organization_membership_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organization_membership_role ON public.organization_membership USING btree (role);


--
-- Name: ix_organization_membership_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organization_membership_user_id ON public.organization_membership USING btree (user_id);


--
-- Name: ix_organization_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organization_status ON public.organization USING btree (status);


--
-- Name: ix_parse_revision_artifact_file_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parse_revision_artifact_file_id ON public.parse_revision USING btree (artifact_file_id);


--
-- Name: ix_parse_revision_identity_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parse_revision_identity_lookup ON public.parse_revision USING btree (artifact_file_id, export_schema_version, parser_provenance_hash, parser_config_hash, reconstruction_config_hash, revision_number);


--
-- Name: ix_parse_revision_reparse_of_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parse_revision_reparse_of_id ON public.parse_revision USING btree (reparse_of_id);


--
-- Name: ix_parse_revision_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parse_revision_status ON public.parse_revision USING btree (status);


--
-- Name: ix_project_invitation_delivery_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_invitation_delivery_status ON public.project_invitation USING btree (delivery_status);


--
-- Name: ix_project_invitation_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_invitation_email ON public.project_invitation USING btree (email);


--
-- Name: ix_project_invitation_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_invitation_expires_at ON public.project_invitation USING btree (expires_at);


--
-- Name: ix_project_invitation_invited_by_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_invitation_invited_by_user_id ON public.project_invitation USING btree (invited_by_user_id);


--
-- Name: ix_project_invitation_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_invitation_project_id ON public.project_invitation USING btree (project_id);


--
-- Name: ix_project_invitation_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_invitation_role ON public.project_invitation USING btree (role);


--
-- Name: ix_project_invitation_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_project_invitation_token_hash ON public.project_invitation USING btree (token_hash);


--
-- Name: ix_project_membership_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_membership_project_id ON public.project_membership USING btree (project_id);


--
-- Name: ix_project_membership_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_membership_role ON public.project_membership USING btree (role);


--
-- Name: ix_project_membership_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_membership_user_id ON public.project_membership USING btree (user_id);


--
-- Name: ix_project_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_organization_id ON public.project USING btree (organization_id);


--
-- Name: ix_project_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_status ON public.project USING btree (status);


--
-- Name: ix_scientific_array_dtype_shape; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scientific_array_dtype_shape ON public.scientific_array USING btree (dtype, shape);


--
-- Name: ix_scientific_array_frame_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scientific_array_frame_id ON public.scientific_array USING btree (frame_id);


--
-- Name: ix_scientific_array_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scientific_array_kind ON public.scientific_array USING btree (kind);


--
-- Name: ix_storage_garbage_collection_run_started_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_storage_garbage_collection_run_started_at ON public.storage_garbage_collection_run USING btree (started_at);


--
-- Name: ix_storage_garbage_collection_run_state_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_storage_garbage_collection_run_state_id ON public.storage_garbage_collection_run USING btree (state_id);


--
-- Name: ix_storage_garbage_collection_run_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_storage_garbage_collection_run_status ON public.storage_garbage_collection_run USING btree (status);


--
-- Name: ix_transition_state_endpoint_calculation_frame_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transition_state_endpoint_calculation_frame_id ON public.transition_state_endpoint USING btree (calculation_frame_id);


--
-- Name: ix_transition_state_endpoint_topology_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transition_state_endpoint_topology_id ON public.transition_state_endpoint USING btree (topology_id);


--
-- Name: ix_transition_state_inference_artifact_ingestion_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transition_state_inference_artifact_ingestion_id ON public.transition_state_inference USING btree (artifact_ingestion_id);


--
-- Name: ix_transition_state_inference_calculation_frame_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transition_state_inference_calculation_frame_id ON public.transition_state_inference USING btree (calculation_frame_id);


--
-- Name: ix_transition_state_inference_logical_reaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transition_state_inference_logical_reaction_id ON public.transition_state_inference USING btree (logical_reaction_id);


--
-- Name: ix_transition_state_inference_mapped_reaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transition_state_inference_mapped_reaction_id ON public.transition_state_inference USING btree (mapped_reaction_id);


--
-- Name: ix_transition_state_inference_parse_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transition_state_inference_parse_revision_id ON public.transition_state_inference USING btree (parse_revision_id);


--
-- Name: ix_transition_state_inference_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transition_state_inference_status ON public.transition_state_inference USING btree (status);


--
-- Name: ix_user_account_primary_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_account_primary_email ON public.user_account USING btree (primary_email);


--
-- Name: ix_user_account_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_account_status ON public.user_account USING btree (status);


--
-- Name: ix_workflow_manifest_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workflow_manifest_status ON public.workflow_manifest USING btree (status);


--
-- Name: ix_workflow_manifest_supersedes_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workflow_manifest_supersedes_id ON public.workflow_manifest USING btree (supersedes_id);


--
-- Name: uq_mapped_node_geometry_primary_component; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_mapped_node_geometry_primary_component ON public.mapped_reaction_node_geometry USING btree (mapped_reaction_node_id, component_key) WHERE is_primary;


--
-- Name: atomic_population_series atomic_population_series_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.atomic_population_series
    ADD CONSTRAINT atomic_population_series_result_id_fkey FOREIGN KEY (result_id) REFERENCES public.charge_spin_population_result(id) ON DELETE CASCADE;


--
-- Name: bond_order_result bond_order_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bond_order_result
    ADD CONSTRAINT bond_order_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: calculation_frame calculation_frame_geometry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_frame
    ADD CONSTRAINT calculation_frame_geometry_id_fkey FOREIGN KEY (geometry_id) REFERENCES public.geometry(id) ON DELETE RESTRICT;


--
-- Name: calculation_segment calculation_segment_parse_revision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_segment
    ADD CONSTRAINT calculation_segment_parse_revision_id_fkey FOREIGN KEY (parse_revision_id) REFERENCES public.parse_revision(id) ON DELETE CASCADE;


--
-- Name: calculation_segment calculation_segment_protocol_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_segment
    ADD CONSTRAINT calculation_segment_protocol_id_fkey FOREIGN KEY (protocol_id) REFERENCES public.calculation_protocol(id) ON DELETE RESTRICT;


--
-- Name: calculation_status_result calculation_status_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_status_result
    ADD CONSTRAINT calculation_status_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: charge_spin_population_result charge_spin_population_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.charge_spin_population_result
    ADD CONSTRAINT charge_spin_population_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: electronic_configuration electronic_configuration_electronic_state_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_configuration
    ADD CONSTRAINT electronic_configuration_electronic_state_id_fkey FOREIGN KEY (electronic_state_id) REFERENCES public.electronic_state(id) ON DELETE CASCADE;


--
-- Name: electronic_state_set electronic_state_set_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_state_set
    ADD CONSTRAINT electronic_state_set_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: electronic_state electronic_state_state_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.electronic_state
    ADD CONSTRAINT electronic_state_state_set_id_fkey FOREIGN KEY (state_set_id) REFERENCES public.electronic_state_set(id) ON DELETE CASCADE;


--
-- Name: energy_observation energy_observation_energy_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.energy_observation
    ADD CONSTRAINT energy_observation_energy_result_id_fkey FOREIGN KEY (energy_result_id) REFERENCES public.frame_energy_result(id) ON DELETE CASCADE;


--
-- Name: artifact_file fk_artifact_file_created_by_user_id_user_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_file
    ADD CONSTRAINT fk_artifact_file_created_by_user_id_user_account FOREIGN KEY (created_by_user_id) REFERENCES public.user_account(id) ON DELETE RESTRICT;


--
-- Name: artifact_file fk_artifact_file_project_id_project; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_file
    ADD CONSTRAINT fk_artifact_file_project_id_project FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE RESTRICT;


--
-- Name: artifact_ingestion fk_artifact_ingestion_artifact_file_id_artifact_file; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_ingestion
    ADD CONSTRAINT fk_artifact_ingestion_artifact_file_id_artifact_file FOREIGN KEY (artifact_file_id) REFERENCES public.artifact_file(id) ON DELETE CASCADE;


--
-- Name: audit_event fk_audit_event_actor_user_id_user_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_event
    ADD CONSTRAINT fk_audit_event_actor_user_id_user_account FOREIGN KEY (actor_user_id) REFERENCES public.user_account(id) ON DELETE SET NULL;


--
-- Name: audit_event fk_audit_event_project_id_project; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_event
    ADD CONSTRAINT fk_audit_event_project_id_project FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE SET NULL;


--
-- Name: auth_session fk_auth_session_user_id_user_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_session
    ADD CONSTRAINT fk_auth_session_user_id_user_account FOREIGN KEY (user_id) REFERENCES public.user_account(id) ON DELETE CASCADE;


--
-- Name: calculation_frame fk_calculation_frame_segment_revision; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_frame
    ADD CONSTRAINT fk_calculation_frame_segment_revision FOREIGN KEY (segment_id, parse_revision_id) REFERENCES public.calculation_segment(id, parse_revision_id) ON DELETE CASCADE;


--
-- Name: calculation_frame fk_calculation_frame_topology_derivation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calculation_frame
    ADD CONSTRAINT fk_calculation_frame_topology_derivation FOREIGN KEY (topology_derivation_id) REFERENCES public.molecular_topology_derivation(id) ON DELETE RESTRICT;


--
-- Name: external_identity fk_external_identity_user_id_user_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_identity
    ADD CONSTRAINT fk_external_identity_user_id_user_account FOREIGN KEY (user_id) REFERENCES public.user_account(id) ON DELETE CASCADE;


--
-- Name: manifest_artifact_binding fk_manifest_artifact_binding_source_same_manifest; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_artifact_binding
    ADD CONSTRAINT fk_manifest_artifact_binding_source_same_manifest FOREIGN KEY (workflow_manifest_id, source_geometry_artifact_key) REFERENCES public.manifest_artifact_binding(workflow_manifest_id, artifact_key) ON DELETE RESTRICT;


--
-- Name: mapped_reaction_edge fk_mapped_reaction_edge_source_same_path; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_edge
    ADD CONSTRAINT fk_mapped_reaction_edge_source_same_path FOREIGN KEY (mapped_reaction_id, source_node_id) REFERENCES public.mapped_reaction_node(mapped_reaction_id, id) ON DELETE RESTRICT;


--
-- Name: mapped_reaction_edge fk_mapped_reaction_edge_target_same_path; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_edge
    ADD CONSTRAINT fk_mapped_reaction_edge_target_same_path FOREIGN KEY (mapped_reaction_id, target_node_id) REFERENCES public.mapped_reaction_node(mapped_reaction_id, id) ON DELETE RESTRICT;


--
-- Name: mapped_reaction_edge fk_mapped_reaction_edge_transition_state_same_path; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_edge
    ADD CONSTRAINT fk_mapped_reaction_edge_transition_state_same_path FOREIGN KEY (mapped_reaction_id, transition_state_node_id) REFERENCES public.mapped_reaction_node(mapped_reaction_id, id) ON DELETE RESTRICT;


--
-- Name: mcp_access_token fk_mcp_access_token_user_id_user_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_access_token
    ADD CONSTRAINT fk_mcp_access_token_user_id_user_account FOREIGN KEY (user_id) REFERENCES public.user_account(id) ON DELETE CASCADE;


--
-- Name: organization_membership fk_organization_membership_organization_id_organization; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_membership
    ADD CONSTRAINT fk_organization_membership_organization_id_organization FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;


--
-- Name: organization_membership fk_organization_membership_user_id_user_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_membership
    ADD CONSTRAINT fk_organization_membership_user_id_user_account FOREIGN KEY (user_id) REFERENCES public.user_account(id) ON DELETE CASCADE;


--
-- Name: parse_revision fk_parse_revision_reparse_of_id_parse_revision; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parse_revision
    ADD CONSTRAINT fk_parse_revision_reparse_of_id_parse_revision FOREIGN KEY (reparse_of_id) REFERENCES public.parse_revision(id) ON DELETE RESTRICT;


--
-- Name: project_invitation fk_project_invitation_invited_by_user_id_user_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_invitation
    ADD CONSTRAINT fk_project_invitation_invited_by_user_id_user_account FOREIGN KEY (invited_by_user_id) REFERENCES public.user_account(id) ON DELETE RESTRICT;


--
-- Name: project_invitation fk_project_invitation_project_id_project; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_invitation
    ADD CONSTRAINT fk_project_invitation_project_id_project FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE CASCADE;


--
-- Name: project_membership fk_project_membership_project_id_project; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_membership
    ADD CONSTRAINT fk_project_membership_project_id_project FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE CASCADE;


--
-- Name: project_membership fk_project_membership_user_id_user_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_membership
    ADD CONSTRAINT fk_project_membership_user_id_user_account FOREIGN KEY (user_id) REFERENCES public.user_account(id) ON DELETE CASCADE;


--
-- Name: project fk_project_organization_id_organization; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project
    ADD CONSTRAINT fk_project_organization_id_organization FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;


--
-- Name: storage_garbage_collection_run fk_storage_gc_run_state_id_storage_gc_state; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_garbage_collection_run
    ADD CONSTRAINT fk_storage_gc_run_state_id_storage_gc_state FOREIGN KEY (state_id) REFERENCES public.storage_garbage_collection_state(id) ON DELETE CASCADE;


--
-- Name: molecular_topology_derivation fk_topology_derivation_topology; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_topology_derivation
    ADD CONSTRAINT fk_topology_derivation_topology FOREIGN KEY (topology_id) REFERENCES public.molecular_topology(id) ON DELETE RESTRICT;


--
-- Name: transition_state_endpoint fk_transition_state_endpoint_frame; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_endpoint
    ADD CONSTRAINT fk_transition_state_endpoint_frame FOREIGN KEY (calculation_frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: transition_state_endpoint fk_transition_state_endpoint_topology; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_endpoint
    ADD CONSTRAINT fk_transition_state_endpoint_topology FOREIGN KEY (topology_id) REFERENCES public.molecular_topology(id) ON DELETE RESTRICT;


--
-- Name: transition_state_inference fk_transition_state_inference_parse_revision_id_parse_revision; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_inference
    ADD CONSTRAINT fk_transition_state_inference_parse_revision_id_parse_revision FOREIGN KEY (parse_revision_id) REFERENCES public.parse_revision(id) ON DELETE CASCADE;


--
-- Name: transition_state_inference fk_ts_inference_calculation_frame; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_inference
    ADD CONSTRAINT fk_ts_inference_calculation_frame FOREIGN KEY (calculation_frame_id) REFERENCES public.calculation_frame(id) ON DELETE RESTRICT;


--
-- Name: transition_state_inference fk_ts_inference_ingestion; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_inference
    ADD CONSTRAINT fk_ts_inference_ingestion FOREIGN KEY (artifact_ingestion_id) REFERENCES public.artifact_ingestion(id) ON DELETE CASCADE;


--
-- Name: transition_state_inference fk_ts_inference_logical_reaction; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_inference
    ADD CONSTRAINT fk_ts_inference_logical_reaction FOREIGN KEY (logical_reaction_id) REFERENCES public.logical_reaction(id) ON DELETE RESTRICT;


--
-- Name: transition_state_inference fk_ts_inference_mapped_reaction; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transition_state_inference
    ADD CONSTRAINT fk_ts_inference_mapped_reaction FOREIGN KEY (mapped_reaction_id) REFERENCES public.mapped_reaction(id) ON DELETE RESTRICT;


--
-- Name: workflow_manifest fk_workflow_manifest_supersedes_same_series; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_manifest
    ADD CONSTRAINT fk_workflow_manifest_supersedes_same_series FOREIGN KEY (manifest_key, supersedes_id) REFERENCES public.workflow_manifest(manifest_key, id) ON DELETE RESTRICT;


--
-- Name: frame_energy_result frame_energy_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.frame_energy_result
    ADD CONSTRAINT frame_energy_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: geometry_optimization_result geometry_optimization_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geometry_optimization_result
    ADD CONSTRAINT geometry_optimization_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: geometry geometry_topology_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geometry
    ADD CONSTRAINT geometry_topology_id_fkey FOREIGN KEY (topology_id) REFERENCES public.molecular_topology(id) ON DELETE RESTRICT;


--
-- Name: implicit_solvation_result implicit_solvation_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.implicit_solvation_result
    ADD CONSTRAINT implicit_solvation_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: logical_reaction_participant logical_reaction_participant_logical_reaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.logical_reaction_participant
    ADD CONSTRAINT logical_reaction_participant_logical_reaction_id_fkey FOREIGN KEY (logical_reaction_id) REFERENCES public.logical_reaction(id) ON DELETE CASCADE;


--
-- Name: logical_reaction_participant logical_reaction_participant_topology_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.logical_reaction_participant
    ADD CONSTRAINT logical_reaction_participant_topology_id_fkey FOREIGN KEY (topology_id) REFERENCES public.molecular_topology(id) ON DELETE RESTRICT;


--
-- Name: manifest_artifact_binding manifest_artifact_binding_artifact_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_artifact_binding
    ADD CONSTRAINT manifest_artifact_binding_artifact_file_id_fkey FOREIGN KEY (artifact_file_id) REFERENCES public.artifact_file(id) ON DELETE RESTRICT;


--
-- Name: manifest_artifact_binding manifest_artifact_binding_workflow_manifest_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.manifest_artifact_binding
    ADD CONSTRAINT manifest_artifact_binding_workflow_manifest_id_fkey FOREIGN KEY (workflow_manifest_id) REFERENCES public.workflow_manifest(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction_edge mapped_reaction_edge_mapped_reaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_edge
    ADD CONSTRAINT mapped_reaction_edge_mapped_reaction_id_fkey FOREIGN KEY (mapped_reaction_id) REFERENCES public.mapped_reaction(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction mapped_reaction_logical_reaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction
    ADD CONSTRAINT mapped_reaction_logical_reaction_id_fkey FOREIGN KEY (logical_reaction_id) REFERENCES public.logical_reaction(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction_node_geometry mapped_reaction_node_geometry_geometry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry
    ADD CONSTRAINT mapped_reaction_node_geometry_geometry_id_fkey FOREIGN KEY (geometry_id) REFERENCES public.geometry(id) ON DELETE RESTRICT;


--
-- Name: mapped_reaction_node_geometry_mapping mapped_reaction_node_geometry_mapped_reaction_node_geometr_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry_mapping
    ADD CONSTRAINT mapped_reaction_node_geometry_mapped_reaction_node_geometr_fkey FOREIGN KEY (mapped_reaction_node_geometry_id) REFERENCES public.mapped_reaction_node_geometry(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction_node_geometry mapped_reaction_node_geometry_mapped_reaction_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry
    ADD CONSTRAINT mapped_reaction_node_geometry_mapped_reaction_node_id_fkey FOREIGN KEY (mapped_reaction_node_id) REFERENCES public.mapped_reaction_node(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction_node_geometry mapped_reaction_node_geometry_mapped_reaction_participant__fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node_geometry
    ADD CONSTRAINT mapped_reaction_node_geometry_mapped_reaction_participant__fkey FOREIGN KEY (mapped_reaction_participant_id) REFERENCES public.mapped_reaction_participant(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction_node mapped_reaction_node_mapped_reaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_node
    ADD CONSTRAINT mapped_reaction_node_mapped_reaction_id_fkey FOREIGN KEY (mapped_reaction_id) REFERENCES public.mapped_reaction(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction_participant mapped_reaction_participant_logical_reaction_participant_i_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_participant
    ADD CONSTRAINT mapped_reaction_participant_logical_reaction_participant_i_fkey FOREIGN KEY (logical_reaction_participant_id) REFERENCES public.logical_reaction_participant(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction_participant mapped_reaction_participant_mapped_reaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_participant
    ADD CONSTRAINT mapped_reaction_participant_mapped_reaction_id_fkey FOREIGN KEY (mapped_reaction_id) REFERENCES public.mapped_reaction(id) ON DELETE CASCADE;


--
-- Name: mapped_reaction_thermodynamic_profile mapped_reaction_thermodynamic_profile_mapped_reaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mapped_reaction_thermodynamic_profile
    ADD CONSTRAINT mapped_reaction_thermodynamic_profile_mapped_reaction_id_fkey FOREIGN KEY (mapped_reaction_id) REFERENCES public.mapped_reaction(id) ON DELETE CASCADE;


--
-- Name: molecular_orbital_result molecular_orbital_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_orbital_result
    ADD CONSTRAINT molecular_orbital_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: molecular_topology molecular_topology_formula_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.molecular_topology
    ADD CONSTRAINT molecular_topology_formula_id_fkey FOREIGN KEY (formula_id) REFERENCES public.molecular_formula(id) ON DELETE RESTRICT;


--
-- Name: multireference_result multireference_result_electronic_state_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.multireference_result
    ADD CONSTRAINT multireference_result_electronic_state_set_id_fkey FOREIGN KEY (electronic_state_set_id) REFERENCES public.electronic_state_set(id) ON DELETE RESTRICT;


--
-- Name: multireference_result multireference_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.multireference_result
    ADD CONSTRAINT multireference_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: nmr_result nmr_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nmr_result
    ADD CONSTRAINT nmr_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: nmr_shielding_tensor nmr_shielding_tensor_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nmr_shielding_tensor
    ADD CONSTRAINT nmr_shielding_tensor_result_id_fkey FOREIGN KEY (result_id) REFERENCES public.nmr_result(id) ON DELETE CASCADE;


--
-- Name: parse_revision parse_revision_artifact_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parse_revision
    ADD CONSTRAINT parse_revision_artifact_file_id_fkey FOREIGN KEY (artifact_file_id) REFERENCES public.artifact_file(id) ON DELETE RESTRICT;


--
-- Name: polarizability_result polarizability_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.polarizability_result
    ADD CONSTRAINT polarizability_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_atomic_population_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_atomic_population_series_id_fkey FOREIGN KEY (atomic_population_series_id) REFERENCES public.atomic_population_series(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_bond_order_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_bond_order_result_id_fkey FOREIGN KEY (bond_order_result_id) REFERENCES public.bond_order_result(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_electronic_state_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_electronic_state_id_fkey FOREIGN KEY (electronic_state_id) REFERENCES public.electronic_state(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_molecular_orbital_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_molecular_orbital_result_id_fkey FOREIGN KEY (molecular_orbital_result_id) REFERENCES public.molecular_orbital_result(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_nmr_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_nmr_result_id_fkey FOREIGN KEY (nmr_result_id) REFERENCES public.nmr_result(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_nmr_shielding_tensor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_nmr_shielding_tensor_id_fkey FOREIGN KEY (nmr_shielding_tensor_id) REFERENCES public.nmr_shielding_tensor(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_polarizability_result_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_polarizability_result_id_fkey FOREIGN KEY (polarizability_result_id) REFERENCES public.polarizability_result(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_scientific_array_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_scientific_array_id_fkey FOREIGN KEY (scientific_array_id) REFERENCES public.scientific_array(id) ON DELETE CASCADE;


--
-- Name: scientific_array_assignment scientific_array_assignment_single_point_property_result_i_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array_assignment
    ADD CONSTRAINT scientific_array_assignment_single_point_property_result_i_fkey FOREIGN KEY (single_point_property_result_id) REFERENCES public.single_point_property_result(id) ON DELETE CASCADE;


--
-- Name: scientific_array scientific_array_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scientific_array
    ADD CONSTRAINT scientific_array_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: single_point_property_result single_point_property_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.single_point_property_result
    ADD CONSTRAINT single_point_property_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: thermochemistry_result thermochemistry_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thermochemistry_result
    ADD CONSTRAINT thermochemistry_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: total_spin_result total_spin_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.total_spin_result
    ADD CONSTRAINT total_spin_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: vibration_result vibration_result_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vibration_result
    ADD CONSTRAINT vibration_result_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES public.calculation_frame(id) ON DELETE CASCADE;


--
-- Name: workflow_manifest workflow_manifest_artifact_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_manifest
    ADD CONSTRAINT workflow_manifest_artifact_file_id_fkey FOREIGN KEY (artifact_file_id) REFERENCES public.artifact_file(id) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--
